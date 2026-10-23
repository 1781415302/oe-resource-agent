"""Versioned tool envelopes and a strict, dependency-free schema subset."""
import json
import math
import time
import uuid

SCHEMA_VERSION = 1


class ToolError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def validate(value, schema, path="input"):
    """Validate the subset used by shipped interfaces; reject unsupported schemas."""
    allowed = {"type", "properties", "required", "additionalProperties", "items", "enum",
               "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems",
               "description", "default"}
    if set(schema) - allowed:
        raise ToolError("SCHEMA_UNSUPPORTED", "接口包含未支持的校验规则。")
    kind = schema.get("type")
    predicates = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if kind not in predicates or not predicates[kind](value):
        raise ToolError("INVALID_INPUT", f"{path} 必须是 {kind}。")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError("INVALID_INPUT", f"{path} 不在可选值中。")
    if kind == "object":
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        unknown = set(value) - set(properties)
        if missing or (unknown and schema.get("additionalProperties") is False):
            raise ToolError("INVALID_INPUT", f"{path} 字段不符。", {"missing": sorted(missing), "unknown": sorted(unknown)})
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], f"{path}.{key}")
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10000):
            raise ToolError("INVALID_INPUT", f"{path} 数量超出范围。")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 65536):
            raise ToolError("INVALID_INPUT", f"{path} 文本长度超出范围。")
    elif kind in ("integer", "number"):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ToolError("INVALID_INPUT", f"{path} 数值超出范围。")


def object_schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


class Registry:
    def __init__(self, event_sink=None):
        self.tools = {}
        self.event_sink = event_sink

    def register(self, name, description, input_schema, handler, mutates=False):
        if name in self.tools:
            raise ValueError(f"Duplicate tool: {name}")
        self.tools[name] = {"name": name, "description": description,
                            "inputSchema": input_schema, "mutates": mutates, "handler": handler}

    def list(self):
        return [{key: value for key, value in tool.items() if key != "handler"} for tool in self.tools.values()]

    def call(self, name, arguments, request_id=None):
        request_id = str(uuid.uuid4()) if request_id is None else request_id
        started = time.monotonic()
        valid_name = isinstance(name, str) and 1 <= len(name) <= 128
        valid_id = (isinstance(request_id, (str, int)) and not isinstance(request_id, bool)
                    and (not isinstance(request_id, str) or 1 <= len(request_id) <= 128))
        result = {"schema_version": SCHEMA_VERSION, "request_id": request_id if valid_id else None,
                  "tool": name if valid_name else "invalid"}
        try:
            if not valid_name:
                raise ToolError("INVALID_TOOL_NAME", "工具名称必须是1–128字符的文本。")
            if not valid_id:
                raise ToolError("INVALID_REQUEST_ID", "请求标识必须是有限整数或1–128字符的文本。")
            if name not in self.tools:
                raise ToolError("UNKNOWN_TOOL", "工具未注册。", {"name": name})
            tool = self.tools[name]
            validate(arguments, tool["inputSchema"])
            output = tool["handler"](arguments)
            json.dumps(output, allow_nan=False)
            result.update(ok=True, data=output)
        except ToolError as exc:
            result.update(ok=False, error={"code": exc.code, "message": str(exc), "details": exc.details})
        except (OSError, ValueError, KeyError, TimeoutError, TypeError) as exc:
            result.update(ok=False, error={"code": "EXECUTION_FAILED", "message": str(exc), "details": {}})
        result["elapsed_seconds"] = time.monotonic() - started
        if self.event_sink:
            self.event_sink({"event": "tool_call", **result})
        return result
