import math
def validate_tool_arguments(schema: dict, arguments: dict) -> tuple[bool, str | None]:
    """Validate the JSON-schema subset used by registered tool parameters."""
    if not isinstance(arguments, dict):
        return False, "Tool arguments must be a JSON object."
    properties = schema.get("properties", {})
    if 'properties' not in schema and 'additionalProperties' not in schema:
        properties = {key: {} for key in arguments}
    if not isinstance(properties, dict):
        return False, "Tool schema is invalid."
    required = schema.get("required", [])
    missing = [key for key in required if key not in arguments]
    if missing:
        return False, f"Required argument is missing: {missing[0]}."
    remaining = [4096]

    def nested(spec, value, depth=0):
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 32 or not _matches_schema_type(value, spec.get('type')):
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                return False
            if 'minimum' in spec and value < spec['minimum'] or 'maximum' in spec and value > spec['maximum']:
                return False
        if 'enum' in spec and value not in spec['enum']:
            return False
        if isinstance(value, str) and not spec.get('minLength', 0) <= len(value) <= spec.get('maxLength', 1_000_000):
            return False
        if isinstance(value, list):
            if not spec.get('minItems', 0) <= len(value) <= spec.get('maxItems', 4096):
                return False
            return all(nested(spec.get('items', {}), item, depth + 1) for item in value)
        if isinstance(value, dict):
            props = spec.get('properties', {})
            if any(key not in value for key in spec.get('required', [])):
                return False
            for key, item in value.items():
                if key not in props and spec.get('additionalProperties') is False:
                    return False
                extra = spec.get('additionalProperties', {})
                child = props.get(key, extra if isinstance(extra, dict) else {})
                if not nested(child, item, depth + 1):
                    return False
        return True

    for key, value in arguments.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            # Keep tool signatures strict by default, but honor an explicitly
            # declared schema for map-like top-level arguments as well.
            extra = schema.get('additionalProperties', False)
            if extra is False:
                return False, f"Unknown argument: {key}."
            spec = extra if isinstance(extra, dict) else {}
        if not _matches_schema_type(value, spec.get("type")):
            return False, f"Argument '{key}' has an invalid type."
        if "enum" in spec and value not in spec["enum"]:
            return False, f"Argument '{key}' has an unsupported value."
        if not nested(spec, value):
            return False, f"Argument '{key}' violates its nested schema or size/depth limit."
    return True, None

def _matches_schema_type(value, expected: str | list[str] | None) -> bool:
    if expected is None:
        return True
    expected_types = [expected] if isinstance(expected, str) else expected
    predicates = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    return any(kind in predicates and predicates[kind](value) for kind in expected_types)
