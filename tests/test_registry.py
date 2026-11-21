import pytest
from typing import Optional, List, Dict
from capri.tools.registry import ToolRegistry, _py_type_to_json_type


def test_type_to_json_type():
    assert _py_type_to_json_type(str) == "string"
    assert _py_type_to_json_type(int) == "number"
    assert _py_type_to_json_type(float) == "number"
    assert _py_type_to_json_type(bool) == "boolean"
    assert _py_type_to_json_type(list) == "array"
    assert _py_type_to_json_type(List[str]) == "array"
    assert _py_type_to_json_type(dict) == "object"
    assert _py_type_to_json_type(Dict[str, int]) == "object"
    assert _py_type_to_json_type(Optional[str]) == "string"
    assert _py_type_to_json_type(Optional[int]) == "number"
    assert _py_type_to_json_type(int | None) == "number"


@pytest.mark.asyncio
async def test_registry_sync_tool():
    reg = ToolRegistry()

    @reg.tool(name="calc_add", description="Add two numbers")
    def add(a: int, b: int) -> int:
        return a + b

    schemas = reg.get_schemas()
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "calc_add"
    assert fn["description"] == "Add two numbers"
    assert "a" in fn["parameters"]["properties"]
    assert "b" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["a", "b"]

    result = await reg.execute("calc_add", {"a": 10, "b": 25})
    assert result == "35"


@pytest.mark.asyncio
async def test_registry_async_tool():
    reg = ToolRegistry()

    @reg.tool(name="greet_async")
    async def greet(name: str = "D") -> str:
        return f"Hey, {name}"

    result = await reg.execute("greet_async", '{"name": "D"}')
    assert result == "Hey, D"

    # Default argument fallback
    result_default = await reg.execute("greet_async", {})
    assert result_default == "Hey, D"

    # Null argument fallback when LLM passes {"name": null}
    result_null = await reg.execute("greet_async", {"name": None})
    assert result_null == "Hey, D"

    # Schema allows null for optional arguments to prevent 400 validation error
    schemas = reg.get_schemas()
    props = schemas[0]["function"]["parameters"]["properties"]
    assert props["name"]["type"] == ["string", "null"]

    # Extra unexpected arguments are safely ignored
    result_extra = await reg.execute("greet_async", {"name": "Kai", "unknown_field": 123})
    assert result_extra == "Hey, Kai"



@pytest.mark.asyncio
async def test_registry_missing_tool():
    reg = ToolRegistry()
    res = await reg.execute("non_existent_tool", {})
    assert "не найден" in res


@pytest.mark.asyncio
async def test_registry_parameter_aliases_and_type_coercion():
    reg = ToolRegistry()

    @reg.tool(name="timer_mock")
    async def timer_mock(seconds: int, label: str = "таймер") -> str:
        return f"{label}:{seconds}"

    # String integer coercion and 'duration' alias
    res1 = await reg.execute("timer_mock", {"duration": "120", "label": "tea"})
    assert res1 == "tea:120"

    # Single argument under unknown key matching single required param
    @reg.tool(name="open_mock")
    async def open_mock(app_name: str) -> str:
        return f"opened:{app_name}"

    res2 = await reg.execute("open_mock", {"random_key": "firefox"})
    assert res2 == "opened:firefox"

