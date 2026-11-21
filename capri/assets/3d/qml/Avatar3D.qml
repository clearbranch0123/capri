import QtQuick
import QtQuick3D

Item {
    id: rootItem
    width: 280
    height: 330

    property string avatarState: "idle"
    property string captionText: ""
    property real pulsePhase: 0.0

    // Цвета в зависимости от состояния
    readonly property color colCyan: "#00e5ff"
    readonly property color colPink: "#ff007f"
    readonly property color colGold: "#ffd700"
    readonly property color colGreen: "#39ff14"
    readonly property color colPurple: "#d946ef"

    property color activeColor: {
        if (avatarState === "listening") return colPink;
        if (avatarState === "thinking") return colGold;
        if (avatarState === "speaking") return colGreen;
        if (avatarState === "playful") return colPurple;
        return colCyan;
    }

    // ── 1. ВЕРХНИЙ HUD-БАБЛ / СУБТИТРЫ ─────────────────────────────────────
    Rectangle {
        id: captionBubble
        visible: rootItem.captionText !== ""
        anchors.top: parent.top
        anchors.topMargin: 6
        anchors.horizontalCenter: parent.horizontalCenter
        width: Math.min(parent.width - 20, captionLabel.implicitWidth + 24)
        height: 24
        radius: 12
        color: "#dc0c0e18"
        border.color: rootItem.activeColor
        border.width: 1.2
        z: 10

        Text {
            id: captionLabel
            anchors.centerIn: parent
            text: rootItem.captionText
            color: "#f0f5ff"
            font.family: "monospace"
            font.pixelSize: 11
            font.bold: true
        }
    }

    // ── 2. ОСНОВНОЙ 3D-ВИЮПОРТ ─────────────────────────────────────────────
    View3D {
        id: view3d
        anchors.fill: parent
        anchors.topMargin: 20
        environment: SceneEnvironment {
            clearColor: "transparent"
            backgroundMode: SceneEnvironment.Transparent
        }

        PerspectiveCamera {
            id: camera
            position: Qt.vector3d(0, 1.25, 2.7)
            clipNear: 0.1
            clipFar: 100.0
            eulerRotation.x: -6
        }

        // Ключевой направленный свет
        DirectionalLight {
            eulerRotation.x: -30
            eulerRotation.y: 45
            brightness: 2.2
            color: "#ffffff"
        }

        // Неоновый заполняющий свет (под цвет состояния)
        DirectionalLight {
            eulerRotation.x: 30
            eulerRotation.y: -135
            brightness: 1.8
            color: rootItem.activeColor
        }

        // Нижняя подсветка для кибер-эффекта
        PointLight {
            position: Qt.vector3d(0, 0.4, 0.8)
            brightness: 2.0
            color: rootItem.activeColor
        }

        Node {
            id: modelPivot
            position.y: -0.15

            Untitled5blend {
                id: capriModel
                scale: rootItem.avatarState === "speaking" ? Qt.vector3d(1.15, 1.15, 1.15) : Qt.vector3d(1.1, 1.1, 1.1)
                Behavior on scale { NumberAnimation { duration: 150 } }
            }

            // Плавное покачивание маятником без телепортов
            SequentialAnimation on eulerRotation.y {
                loops: Animation.Infinite
                running: true
                NumberAnimation {
                    to: 12
                    duration: rootItem.avatarState === "thinking" ? 800 : 2500
                    easing.type: Easing.InOutSine
                }
                NumberAnimation {
                    to: -12
                    duration: rootItem.avatarState === "thinking" ? 800 : 2500
                    easing.type: Easing.InOutSine
                }
            }

            SequentialAnimation on position.y {
                loops: Animation.Infinite
                running: true
                NumberAnimation {
                    to: -0.08
                    duration: rootItem.avatarState === "speaking" ? 500 : 1800
                    easing.type: Easing.InOutQuad
                }
                NumberAnimation {
                    to: -0.16
                    duration: rootItem.avatarState === "speaking" ? 500 : 1800
                    easing.type: Easing.InOutQuad
                }
            }

            // Наклон при прослушивании
            eulerRotation.z: rootItem.avatarState === "listening" ? -12 : (rootItem.avatarState === "playful" ? 10 : 0)
            Behavior on eulerRotation.z { NumberAnimation { duration: 300; easing.type: Easing.OutBack } }
        }
    }

    // ── 3. СТАТУС ВНИЗУ (IDLE / TALK / THINK) ──────────────────────────────
    Rectangle {
        id: statusBadge
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 8
        anchors.horizontalCenter: parent.horizontalCenter
        width: 76
        height: 18
        radius: 9
        color: "#e60a0c14"
        border.color: rootItem.activeColor
        border.width: 1.0
        z: 10

        Text {
            anchors.centerIn: parent
            text: rootItem.avatarState.toUpperCase()
            color: rootItem.activeColor
            font.family: "monospace"
            font.pixelSize: 10
            font.bold: true
        }
    }
}
