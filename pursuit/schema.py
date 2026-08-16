"""Workflow DSL: JSON schema, validation, template rendering, safe expressions.

A workflow is a JSON document:

{
  "name": "weekly-report",
  "description": "...",
  "notify": {"email": {"on": ["success"], "to": "boss@x.com"}, "ntfy": {"on": ["error"], "topic": "jorge"}},
  "vars": {"recipient": "boss@x.com"},
  "retries": 1,
  "timeout": 300,
  "on_error": "fail",
  "steps": [
    {"id": "research", "tool": "web", "params": {"query": "sales figures ${topic}"}, "if": "steps.research"},
    {"id": "send", "tool": "email", "params": {"to": "${recipient}", "subject": "Report", "body": "${steps.research.output}"}}
  ]
}

Templates use ${path} placeholders resolved against the execution state, which
exposes: vars (workflow variables), steps.<id>.output / .params / .status, and
task (task id/name/priority). Conditions in "if" are evaluated with a safe,
AST-walking evaluator (no arbitrary code execution).
"""
from __future__ import annotations

import ast
import builtins
import json
import re
from typing import Any

__all__ = [
    "WORKFLOW_SCHEMA",
    "TOOLS",
    "validate_definition",
    "normalize_definition",
    "render",
    "render_params",
    "eval_bool",
]

TOOLS = (
    "shell",
    "read",
    "write",
    "append",
    "list",
    "web",
    "research",
    "email",
    "notify",
    "http",
    "webhook",
    "set",
    "expr",
    "sleep",
    "delegate",
    "memory",
)

WORKFLOW_SCHEMA: dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "https://jorge.local/pursuit/workflow.schema.json",
    "title": "Jorge Pursuit Workflow",
    "type": "object",
    "required": ["name", "steps"],
    "properties": {
        "$schema": {"type": "string"},
        "name": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "notify": {
            "type": "object",
            "description": "Per-channel notification config; see pursuit/notify.py",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "on": {"type": "array", "items": {"enum": ["start", "success", "error", "retry"]}},
                    "to": {"type": "string"},
                    "topic": {"type": "string"},
                    "server": {"type": "string"},
                    "token": {"type": "string"},
                    "url": {"type": "string"},
                    "priority": {"type": "integer"},
                },
            },
        },
        "vars": {"type": "object"},
        "retries": {"type": "integer", "minimum": 0, "default": 0},
        "timeout": {"type": "integer", "minimum": 1, "default": 300},
        "on_error": {"enum": ["fail", "skip", "continue"], "default": "fail"},
        "steps": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "tool"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"},
                    "description": {"type": "string"},
                    "tool": {"enum": TOOLS},
                    "params": {"type": "object"},
                    "if": {"type": "string"},
                    "retries": {"type": "integer", "minimum": 0},
                    "timeout": {"type": "integer", "minimum": 1},
                    "retry_delay": {"type": "number", "minimum": 0},
                    "on_error": {"enum": ["fail", "skip", "continue"]},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def _resolve(path: str, state: dict[str, Any]) -> Any:
    node: Any = state
    for part in path.strip().split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def render(text: Any, state: dict[str, Any]) -> Any:
    if not isinstance(text, str):
        return text

    def sub(m: re.Match[str]) -> str:
        val = _resolve(m.group(1), state)
        if val is None:
            return m.group(0)
        if isinstance(val, (dict, list)):
            return json.dumps(val)
        return str(val)

    return _PLACEHOLDER.sub(sub, text)


def render_params(params: Any, state: dict[str, Any]) -> Any:
    if isinstance(params, dict):
        return {k: render_params(v, state) for k, v in params.items()}
    if isinstance(params, list):
        return [render_params(v, state) for v in params]
    return render(params, state)


_ALLOWED_NODES = (
    ast.Expression, ast.BoolOp, ast.Compare, ast.Constant, ast.Name, ast.Load,
    ast.And, ast.Or, ast.Not, ast.UnaryOp, ast.USub, ast.UAdd,
    ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.Attribute, ast.Subscript, ast.Index, ast.Slice, ast.List, ast.Tuple, ast.Dict,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.Call, ast.keyword,
)

_SAFE_FUNCS = {"int", "float", "str", "bool", "len", "abs", "min", "max", "round"}


def safe_eval(expr: Any, state: dict[str, Any]) -> Any:
    """Evaluate a small, side-effect-free expression against the state dict."""
    if isinstance(expr, (int, float, bool)) or expr is None:
        return expr
    if not isinstance(expr, str) or not expr.strip():
        return expr
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"invalid expression {expr!r}: {e}") from e
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"unsupported construct in expression {expr!r}: {type(node).__name__}")
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS):
                raise ValueError(f"call to disallowed function in expression {expr!r}")
    globals_ns: dict[str, Any] = {
        "__builtins__": {name: getattr(builtins, name) for name in _SAFE_FUNCS},
        "state": state,
        "vars": state.get("vars", {}),
        "steps": state.get("steps", {}),
        "task": state.get("task", {}),
    }
    try:
        return eval(compile(tree, "<expression>", "eval"), globals_ns, {})
    except (KeyError, IndexError, TypeError, AttributeError, NameError, ZeroDivisionError) as e:
        raise ValueError(f"expression {expr!r} could not be evaluated: {e}") from e


def eval_bool(expr: Any, state: dict[str, Any]) -> bool:
    return bool(safe_eval(expr, state))


def validate_definition(defn: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(defn, dict):
        return ["workflow definition must be a JSON object"]
    if not isinstance(defn.get("name"), str) or not defn["name"].strip():
        errors.append("missing required string field: name")
    steps = defn.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("missing required array field: steps (must contain at least one step)")
    else:
        seen: set[str] = set()
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"steps[{i}] must be an object")
                continue
            sid = step.get("id")
            if not isinstance(sid, str) or not sid.strip():
                errors.append(f"steps[{i}] missing required string field: id")
            elif sid in seen:
                errors.append(f"duplicate step id: {sid}")
            else:
                seen.add(sid)
            tool = step.get("tool")
            if tool not in TOOLS:
                errors.append(f"steps[{i}] tool {tool!r} is not supported (choose from: {', '.join(TOOLS)})")
            if "params" in step and not isinstance(step["params"], dict):
                errors.append(f"steps[{i}] params must be an object")
            for opt in ("retries", "timeout"):
                if opt in step and (not isinstance(step[opt], int) or step[opt] < 1):
                    errors.append(f"steps[{i}].{opt} must be a positive integer")
            if "retry_delay" in step and (not isinstance(step["retry_delay"], (int, float)) or step["retry_delay"] < 0):
                errors.append(f"steps[{i}].retry_delay must be a non-negative number")
            if "on_error" in step and step["on_error"] not in ("fail", "skip", "continue"):
                errors.append(f"steps[{i}].on_error must be one of fail|skip|continue")
            unknown = [k for k in step if k not in ("id", "description", "tool", "params", "if", "retries", "timeout", "retry_delay", "on_error")]
            if unknown:
                errors.append(f"steps[{i}] has unknown fields: {', '.join(unknown)}")
    if "retries" in defn and (not isinstance(defn["retries"], int) or defn["retries"] < 0):
        errors.append("retries must be a non-negative integer")
    if "timeout" in defn and (not isinstance(defn["timeout"], int) or defn["timeout"] < 1):
        errors.append("timeout must be a positive integer")
    if "on_error" in defn and defn["on_error"] not in ("fail", "skip", "continue"):
        errors.append("on_error must be one of fail|skip|continue")
    if "vars" in defn and not isinstance(defn["vars"], dict):
        errors.append("vars must be an object")
    if "notify" in defn and not isinstance(defn["notify"], dict):
        errors.append("notify must be an object")
    return errors


def normalize_definition(defn: dict[str, Any]) -> dict[str, Any]:
    errors = validate_definition(defn)
    if errors:
        raise ValueError("invalid workflow definition:\n- " + "\n- ".join(errors))
    out = dict(defn)
    out.setdefault("retries", 0)
    out.setdefault("timeout", 300)
    out.setdefault("on_error", "fail")
    out.setdefault("vars", {})
    out.setdefault("notify", {})
    out.setdefault("description", "")
    for step in out["steps"]:
        step.setdefault("retries", out["retries"])
        step.setdefault("timeout", out["timeout"])
        step.setdefault("on_error", out["on_error"])
        step.setdefault("params", {})
    return out