//! Hardened per-user Unix-socket IPC server, `flock` singleton lock, PID file management,
//! and Linux `SO_PEERCRED` peer UID verification (1:1 contract with `capri/ui/avatar_ipc.py`).

use std::collections::HashMap;
use std::env;
use std::ffi::CString;
use std::fs;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

use crate::protocol::{IpcCommand, IpcStreamBuffer};

fn current_uid() -> u32 {
    // SAFETY: `getuid()` is always safe and thread-safe on Linux.
    unsafe { libc::getuid() }
}

pub fn get_avatar_runtime_dir() -> PathBuf {
    if let Ok(xdg) = env::var("XDG_RUNTIME_DIR") {
        let trimmed = xdg.trim();
        if !trimmed.is_empty() && Path::new(trimmed).is_dir() {
            return PathBuf::from(trimmed).join("capri");
        }
    }
    PathBuf::from(format!("/tmp/capri-{}", current_uid()))
}

pub fn get_avatar_socket_path() -> PathBuf {
    if let Ok(override_path) = env::var("CAPRI_AVATAR_SOCKET") {
        let trimmed = override_path.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    get_avatar_runtime_dir().join("avatar.sock")
}

pub fn get_avatar_pid_path() -> PathBuf {
    if let Ok(override_path) = env::var("CAPRI_AVATAR_PID") {
        let trimmed = override_path.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    get_avatar_runtime_dir().join("avatar.pid")
}

pub fn get_avatar_lock_path(pid_path: &Path) -> PathBuf {
    pid_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(get_avatar_runtime_dir)
        .join("avatar.lock")
}

/// Create and validate the per-user runtime directory (`0700`, non-symlink, owned by current UID).
pub fn ensure_avatar_runtime_dir(target: &Path) -> io::Result<()> {
    if let Ok(meta) = fs::symlink_metadata(target) {
        if meta.file_type().is_symlink() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                format!("Refusing symlinked IPC runtime directory: {}", target.display()),
            ));
        }
    }
    fs::create_dir_all(target)?;
    let meta = fs::symlink_metadata(target)?;
    if meta.file_type().is_symlink() || !meta.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!("Invalid IPC runtime directory path: {}", target.display()),
        ));
    }
    if meta.uid() != current_uid() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            format!(
                "IPC runtime directory owned by uid {}, expected {}: {}",
                meta.uid(),
                current_uid(),
                target.display()
            ),
        ));
    }
    let _ = fs::set_permissions(target, fs::Permissions::from_mode(0o700));
    Ok(())
}

pub fn safe_unlink_owned_path(path: &Path) -> bool {
    let Ok(meta) = fs::symlink_metadata(path) else {
        return false;
    };
    if meta.file_type().is_symlink() || meta.uid() != current_uid() {
        return false;
    }
    fs::remove_file(path).is_ok()
}

pub fn enforce_owner_only_mode(path: &Path, mode: u32) {
    if let Ok(meta) = fs::symlink_metadata(path) {
        if !meta.file_type().is_symlink() && meta.uid() == current_uid() {
            let _ = fs::set_permissions(path, fs::Permissions::from_mode(mode));
        }
    }
}

fn path_to_cstring(path: &Path) -> io::Result<CString> {
    use std::os::unix::ffi::OsStrExt;
    CString::new(path.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "NUL byte in path"))
}

/// Atomically write `pid` to a `0600` regular file without following symlinks (`O_NOFOLLOW`).
pub fn write_owned_pid_file(path: &Path, pid: u32) -> io::Result<()> {
    safe_unlink_owned_path(path);
    let cpath = path_to_cstring(path)?;
    // SAFETY: valid CString pointer and POSIX flags.
    let fd = unsafe {
        libc::open(
            cpath.as_ptr(),
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )
    };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let payload = format!("{pid}\n");
    // SAFETY: valid open file descriptor and buffer slice.
    let written = unsafe {
        libc::write(
            fd,
            payload.as_ptr().cast::<libc::c_void>(),
            payload.len(),
        )
    };
    // SAFETY: closing descriptor opened above.
    unsafe {
        libc::close(fd);
    }
    if written < 0 || (written as usize) != payload.len() {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

/// Non-blocking POSIX `flock` singleton lock held for the lifetime of the avatar daemon.
pub struct AvatarSingletonLock {
    fd: Option<RawFd>,
}

impl AvatarSingletonLock {
    pub fn acquire(lock_path: &Path) -> io::Result<Option<Self>> {
        let cpath = path_to_cstring(lock_path)?;
        // SAFETY: valid CString pointer and POSIX flags.
        let fd = unsafe {
            libc::open(
                cpath.as_ptr(),
                libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }

        let mut st: libc::stat = unsafe { std::mem::zeroed() };
        let rc = unsafe { libc::fstat(fd, &mut st) };
        if rc != 0
            || (st.st_mode & libc::S_IFMT) != libc::S_IFREG
            || st.st_uid != current_uid()
        {
            unsafe {
                libc::close(fd);
            }
            return Ok(None);
        }
        unsafe {
            libc::fchmod(fd, 0o600);
        }
        let lock_rc = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
        if lock_rc != 0 {
            unsafe {
                libc::close(fd);
            }
            return Ok(None);
        }
        Ok(Some(Self { fd: Some(fd) }))
    }
}

impl Drop for AvatarSingletonLock {
    fn drop(&mut self) {
        if let Some(fd) = self.fd.take() {
            unsafe {
                libc::flock(fd, libc::LOCK_UN);
                libc::close(fd);
            }
        }
    }
}

/// Verify via Linux `SO_PEERCRED` that the connected Unix socket peer runs as the current UID.
pub fn peer_socket_matches_current_uid(stream: &UnixStream) -> bool {
    let mut cred: libc::ucred = libc::ucred {
        pid: 0,
        uid: u32::MAX,
        gid: u32::MAX,
    };
    let mut len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    // SAFETY: `cred` and `len` match `SO_PEERCRED` contract on Linux.
    let rc = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            (&mut cred as *mut libc::ucred).cast::<libc::c_void>(),
            &mut len,
        )
    };
    rc == 0 && (len as usize) == std::mem::size_of::<libc::ucred>() && cred.uid == current_uid()
}

struct ClientConn {
    stream: UnixStream,
    buf: IpcStreamBuffer,
}

/// Non-blocking Unix socket IPC server driven directly from the main `poll()` loop.
pub struct AvatarIpcServer {
    sock_path: PathBuf,
    listener: UnixListener,
    clients: HashMap<RawFd, ClientConn>,
}

impl AvatarIpcServer {
    pub fn bind(sock_path: PathBuf) -> io::Result<Self> {
        if let Some(parent) = sock_path.parent() {
            ensure_avatar_runtime_dir(parent)?;
        }
        safe_unlink_owned_path(&sock_path);
        let listener = UnixListener::bind(&sock_path)?;
        enforce_owner_only_mode(&sock_path, 0o600);
        listener.set_nonblocking(true)?;
        Ok(Self {
            sock_path,
            listener,
            clients: HashMap::new(),
        })
    }

    pub fn listener_fd(&self) -> RawFd {
        self.listener.as_raw_fd()
    }

    pub fn client_fds(&self) -> Vec<RawFd> {
        self.clients.keys().copied().collect()
    }

    pub fn accept_pending(&mut self) {
        loop {
            match self.listener.accept() {
                Ok((stream, _addr)) => {
                    if !peer_socket_matches_current_uid(&stream) {
                        drop(stream);
                        continue;
                    }
                    if stream.set_nonblocking(true).is_err() {
                        drop(stream);
                        continue;
                    }
                    let fd = stream.as_raw_fd();
                    self.clients.insert(
                        fd,
                        ClientConn {
                            stream,
                            buf: IpcStreamBuffer::default(),
                        },
                    );
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(_) => break,
            }
        }
    }

    pub fn read_client(&mut self, fd: RawFd) -> Vec<IpcCommand> {
        let mut cmds = Vec::new();
        let mut remove = false;
        if let Some(conn) = self.clients.get_mut(&fd) {
            let mut tmp = [0u8; 4096];
            loop {
                match conn.stream.read(&mut tmp) {
                    Ok(0) => {
                        if let Some(last) = conn.buf.flush_eof() {
                            cmds.push(last);
                        }
                        remove = true;
                        break;
                    }
                    Ok(n) => {
                        cmds.extend(conn.buf.feed(&tmp[..n]));
                    }
                    Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                    Err(_) => {
                        remove = true;
                        break;
                    }
                }
            }
        }
        if remove {
            self.clients.remove(&fd);
        }
        cmds
    }
}

impl Drop for AvatarIpcServer {
    fn drop(&mut self) {
        self.clients.clear();
        safe_unlink_owned_path(&self.sock_path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_tmp_dir(tag: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        std::env::temp_dir().join(format!("capri-rs-test-{tag}-{}-{nanos}", std::process::id()))
    }

    #[test]
    fn singleton_lock_blocks_second_holder_and_releases_on_drop() {
        let dir = unique_tmp_dir("lock");
        ensure_avatar_runtime_dir(&dir).expect("create runtime dir");
        let lock_path = dir.join("avatar.lock");

        let first = AvatarSingletonLock::acquire(&lock_path)
            .expect("acquire 1")
            .expect("first holder must succeed");
        let second = AvatarSingletonLock::acquire(&lock_path).expect("acquire 2");
        assert!(second.is_none(), "second holder must be rejected while held");
        drop(first);

        let third = AvatarSingletonLock::acquire(&lock_path).expect("acquire 3");
        assert!(third.is_some(), "lock must be acquirable after drop");
        drop(third);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn ipc_server_roundtrip_and_peercred_and_permissions() {
        let dir = unique_tmp_dir("ipc");
        let sock_path = dir.join("avatar.sock");
        let mut srv = AvatarIpcServer::bind(sock_path.clone()).expect("bind ipc");

        let meta = fs::symlink_metadata(&sock_path).expect("stat socket");
        assert_eq!(meta.permissions().mode() & 0o777, 0o600);

        let mut client = UnixStream::connect(&sock_path).expect("connect");
        assert!(peer_socket_matches_current_uid(&client));
        client
            .write_all(b"{\"v\":1,\"state\":\"speaking\",\"text\":\"Hello\"}\n")
            .expect("write");
        drop(client);

        srv.accept_pending();
        let fds = srv.client_fds();
        assert_eq!(fds.len(), 1);
        let cmds = srv.read_client(fds[0]);
        assert_eq!(
            cmds,
            vec![IpcCommand::StateUpdate {
                state: Some("speaking".into()),
                text: Some("Hello".into()),
                pose: None,
                mood: None,
                mouth_envelope: None,
            }]
        );
        drop(srv);
        assert!(!sock_path.exists(), "socket unlinked on drop");
        let _ = fs::remove_dir_all(&dir);
    }
}
