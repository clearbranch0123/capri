import inspect
import json
import logging
import types
import typing
from typing import Any, Callable, Dict, List, Optional, get_type_hints, get_origin, get_args

logger = logging.getLogger(__name__)


def _py_type_to_json_type(py_type: Any) -> str:
    origin = get_origin(py_type)
    if origin in (types.UnionType, typing.Union):
        args = [a for a in get_args(py_type) if a is not type(None)]
        if args:
            py_type = args[0]
            origin = get_origin(py_type)

    if origin in (list, List):
        return "array"
    if origin in (dict, Dict):
        return "object"
    if py_type in (str,):
        return "string"
    if py_type in (int, float):
        return "number"
    if py_type in (bool,):
        return "boolean"
    if py_type in (list, List):
        return "array"
    if py_type in (dict, Dict):
        return "object"
    return "string"


class ToolRegistry:
    def __init__(self, default_policy_engine: Any = None):
        self._tools: Dict[str, Callable] = {}
        self._builtin_tools: set[str] = set()
        self._schemas: List[Dict[str, Any]] = []
        self._default_policy_engine: Any = default_policy_engine

    def set_default_policy_engine(self, policy_engine: Any) -> None:
        self._default_policy_engine = policy_engine

    def register(self, func: Callable, name: str = None, description: str = None) -> Callable:
        tool_name = name or func.__name__
        tool_doc = description or (inspect.getdoc(func) or "").strip()

        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            param_type = type_hints.get(param_name, str)
            json_type = _py_type_to_json_type(param_type)
            is_required = (param.default is inspect.Parameter.empty)

            origin = get_origin(param_type)
            is_nullable = not is_required or (
                origin in (types.UnionType, typing.Union) and type(None) in get_args(param_type)
            )

            # Для необязательных параметров разрешаем null в схеме,
            # чтобы валидатор LLM (Groq/OpenAI) не отклонял запросы с ошибкой 400
            type_def = [json_type, "null"] if is_nullable else json_type

            properties[param_name] = {
                "type": type_def,
            }

            if is_required:
                required.append(param_name)


        schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_doc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

        self._tools[tool_name] = func
        self._builtin_tools.add(tool_name)
        self._schemas = [s for s in self._schemas if s.get("function", {}).get("name") != tool_name]
        self._schemas.append(schema)
        return func

    def register_external_tool(
        self,
        tool_name: str,
        func: Callable,
        description: str,
        parameters_schema: Optional[Dict[str, Any]] = None,
    ) -> Callable:
        """Register an external dynamic tool (e.g. MCP server tool) with an explicit JSON Schema."""
        if not tool_name or not tool_name.startswith("mcp__"):
            raise ValueError(f"External tool name '{tool_name}' must use the 'mcp__' namespace prefix.")
        if tool_name in self._builtin_tools:
            raise ValueError(f"External tool '{tool_name}' cannot overwrite built-in tool.")
        params = parameters_schema if isinstance(parameters_schema, dict) else {"type": "object", "properties": {}, "required": []}
        if "type" not in params:
            params = {"type": "object", **params}
        schema = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": (description or tool_name).strip(),
                "parameters": params,
            },
        }
        self._tools[tool_name] = func
        self._schemas = [s for s in self._schemas if s.get("function", {}).get("name") != tool_name]
        self._schemas.append(schema)
        return func

    def unregister_prefix(self, prefix: str) -> int:
        """Remove all registered tools whose name starts with `prefix` (e.g. 'mcp__')."""
        removed = [name for name in list(self._tools.keys()) if name.startswith(prefix)]
        for name in removed:
            self._tools.pop(name, None)
        if removed:
            removed_set = set(removed)
            self._schemas = [
                s for s in self._schemas if s.get("function", {}).get("name") not in removed_set
            ]
        return len(removed)

    def tool(self, name: str = None, description: str = None):
        def decorator(func: Callable):
            return self.register(func, name=name, description=description)
        return decorator

    def get_schemas(self) -> List[Dict[str, Any]]:
        return self._schemas

    async def execute(self, tool_name: str, arguments: Dict[str, Any] | str, policy_engine: Any = None) -> str:
        if tool_name not in self._tools:
            if tool_name.lower() in ("appraisal", "thought", "emotion"):
                return "Внимание: appraisal/thought — это внутренний XML-тег в начале текстового ответа (<appraisal>...</appraisal>), а не системный инструмент. Сформируй ответ текстом."
            return f"Ошибка: Инструмент '{tool_name}' не найден."

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except Exception as e:
                return f"Ошибка парсинга аргументов: {e}"

        func = self._tools[tool_name]
        sig = inspect.signature(func)
        clean_args = {}

        # Словарь частых синонимов параметров, которые генерируют различные LLM
        common_aliases = {
            "fact": ["value", "text", "content", "val", "info", "data", "note"],
            "target": ["app_name", "name", "app", "process", "window", "title", "query", "pkg"],
            "app_name": ["target", "name", "app", "package"],
            "seconds": ["duration", "time", "sec", "secs", "timeout"],
            "query": ["text", "search", "q", "keyword", "target", "prompt"],
            "url": ["link", "uri", "site", "address"],
            "message": ["text", "msg", "content", "body", "description"],
            "title": ["header", "name", "subject", "summary"],
            "command": ["cmd", "bash", "shell", "script"],
            "level": ["volume", "val", "value", "percent"],
            "pattern": ["query", "filter", "search", "glob"],
            "path": ["file_path", "filepath", "filename", "dir", "directory"],
            "city": ["location", "place", "town"],
        }

        # Фильтруем, маппим синонимы и нормализуем типы аргументов под сигнатуру функции
        req_params_count = len([p for p in sig.parameters.values() if p.default is inspect.Parameter.empty and p.name not in ("self", "cls")])
        for p_name, p_param in sig.parameters.items():
            if p_name in ("self", "cls"):
                continue

            val = None
            if p_name in arguments:
                val = arguments[p_name]
            else:
                # Проверяем возможные синонимы
                for alias in common_aliases.get(p_name, []):
                    if alias in arguments:
                        val = arguments[alias]
                        break
                # Если передан ровно один аргумент под неизвестным ключом, а функция ждет один обязательный параметр
                if val is None and len(arguments) == 1 and req_params_count == 1:
                    val = list(arguments.values())[0]

            # Если передан null, а в функции есть значение по умолчанию — подставляем дефолт
            if val is None and p_param.default is not inspect.Parameter.empty:
                val = p_param.default

            # Приведение типов для строгой надежности
            if val is not None:
                if p_param.annotation in (int, "int"):
                    try:
                        val = int(float(str(val).strip()))
                    except Exception:
                        pass
                elif p_param.annotation in (float, "float"):
                    try:
                        val = float(str(val).strip())
                    except Exception:
                        pass
                elif p_param.annotation in (bool, "bool") and isinstance(val, str):
                    val = val.lower() in ("true", "1", "yes", "on", "y")
                elif p_param.annotation in (str, "str") and not isinstance(val, str):
                    val = str(val)

            if val is not None or p_param.default is not inspect.Parameter.empty:
                clean_args[p_name] = val

        # Если функция принимает **kwargs — передаем остальные аргументы
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
        if has_varkw:
            for k, v in arguments.items():
                if k not in clean_args:
                    clean_args[k] = v

        # Централизованная проверка политики безопасности перед выполнением инструмента.
        # Если policy_engine явно не передан, используется защитная политика по умолчанию (fail-closed).
        effective_policy = policy_engine if policy_engine is not None else self._default_policy_engine
        if effective_policy is not None:
            try:
                allowed, decision = await effective_policy.authorize(tool_name, clean_args)
            except Exception as pe:
                logger.error(f"Tool policy check failed for {tool_name}: {pe}", exc_info=True)
                return f"Выполнение инструмента '{tool_name}' отклонено политикой безопасности: {pe}"
            if not allowed:
                return (
                    f"ВНИМАНИЕ (политика безопасности [{decision.risk}]): "
                    f"выполнение '{tool_name}' заблокировано или отклонено. Причина: {decision.reason}"
                )

        try:
            if inspect.iscoroutinefunction(func):
                result = await func(**clean_args)
            else:
                result = func(**clean_args)
            return str(result)
        except Exception as e:
            from ..core.doctor import redact_sensitive_text
            safe_err = redact_sensitive_text(str(e))
            logger.error(f"Error executing tool {tool_name}: {safe_err}")
            return f"Ошибка при выполнении {tool_name}: {safe_err}"



# Global registry instance with fail-closed default policy engine (cannot bypass policy when called directly)
from ..core.policy import ToolPolicyEngine

registry = ToolRegistry(default_policy_engine=ToolPolicyEngine(mode="ask"))
tool = registry.tool
