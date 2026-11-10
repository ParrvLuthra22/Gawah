"""Opt-in, redacted comparison of minimal and shopping-agent Groq tool calls."""

import argparse
import asyncio
import json
from typing import Any

from groq import AsyncGroq, BadRequestError
from shopper.agent.shopping_agent import TOOL_DEFINITIONS, agent_system_prompt
from shopper.config import Settings
from shopper.integrations.groq_client import MODEL_PRIORITY
from shopper.models.shopping_intent import ShoppingIntent


def _safe_error(exc: BadRequestError) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    error = body.get("error", body)
    if not isinstance(error, dict):
        error = {}
    failed_generation = error.get("failed_generation")
    failed_text = failed_generation if isinstance(failed_generation, str) else ""
    return {
        "status": exc.status_code,
        "code": error.get("code"),
        "type": error.get("type"),
        "param": error.get("param"),
        "failed_generation_present": failed_generation is not None,
        "failed_generation_tool_markers": [
            name
            for name in ("search_products", "get_product_details", "compare_products")
            if name in failed_text
        ],
    }


async def _select_model(client: AsyncGroq, settings: Settings) -> str:
    if settings.groq_model.lower() != "auto":
        return settings.groq_model
    page = await client.models.list()
    active = {
        model.id
        for model in page.data
        if getattr(model, "active", True) is not False
    }
    for model in MODEL_PRIORITY:
        if model in active:
            return model
    raise RuntimeError("No configured Groq model is available")


def _safe_message_summary(message: Any, finish_reason: str | None) -> dict[str, Any]:
    calls = message.tool_calls or []
    return {
        "finish_reason": finish_reason,
        "tool_call_count": len(calls),
        "tool_names": [call.function.name for call in calls],
        "has_content": bool(message.content),
    }


async def _request(
    client: AsyncGroq,
    model: str,
    label: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: str | dict[str, Any] = "auto",
) -> Any | None:
    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            parallel_tool_calls=False,
            temperature=0,
        )
    except BadRequestError as exc:
        print(json.dumps({"label": label, "result": "bad_request", **_safe_error(exc)}))
        return None
    except Exception as exc:  # noqa: BLE001 - diagnostic must redact unexpected SDK failures.
        print(json.dumps({"label": label, "result": "error", "exception_type": type(exc).__name__}))
        return None

    choice = completion.choices[0]
    print(json.dumps({"label": label, "result": "ok", **_safe_message_summary(choice.message, choice.finish_reason)}))
    return choice.message


def _assistant_transcript(message: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build the same safe assistant/tool transcript used by the shopping agent."""
    tool_calls = message.tool_calls or []
    assistant_calls = [
        {
            "id": call.id,
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }
        for call in tool_calls
    ]
    assistant: dict[str, Any] = {"role": "assistant", "tool_calls": assistant_calls}
    if message.content:
        assistant["content"] = message.content
    tools = [
        {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.function.name,
            "content": '{"products":[]}',
        }
        for call in tool_calls
    ]
    return assistant, tools


async def main() -> None:
    settings = Settings()
    client = AsyncGroq(api_key=settings.groq_api_key, timeout=15.0, max_retries=0)
    model = await _select_model(client, settings)
    print(json.dumps({"model": model, "parallel_tool_calls": False}))

    minimal_tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Return a static lookup result.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    await _request(
        client,
        model,
        "minimal_turn_1",
        [{"role": "user", "content": "Use lookup once, then respond."}],
        minimal_tools,
    )

    diagnostic_intent = ShoppingIntent(
        category="shoes",
        brand="Nike",
        color="black",
        size="12",
        max_budget=60,
        currency="USD",
    )
    agent_messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent_system_prompt(diagnostic_intent)}
    ]
    no_final_json_messages = [
        {
            "role": "system",
            "content": agent_system_prompt(diagnostic_intent).replace(
                "End with JSON only: selected_product_id (or null) and a concise explanation.\n",
                "",
            ),
        }
    ]
    no_intent_messages = [
        {
            "role": "system",
            "content": agent_system_prompt(diagnostic_intent).split("\nIntent: ", maxsplit=1)[0],
        }
    ]

    await _request(client, model, "agent_prompt_minimal_tool", agent_messages, minimal_tools)
    await _request(
        client,
        model,
        "simple_user_agent_tools",
        [{"role": "user", "content": "Use search_products once, then respond."}],
        TOOL_DEFINITIONS,
    )
    await _request(
        client,
        model,
        "agent_prompt_without_intent",
        no_intent_messages,
        TOOL_DEFINITIONS,
    )
    await _request(
        client,
        model,
        "agent_prompt_search_tool",
        agent_messages,
        TOOL_DEFINITIONS[:1],
    )
    await _request(
        client,
        model,
        "agent_prompt_search_details_tools",
        agent_messages,
        TOOL_DEFINITIONS[:2],
    )
    await _request(
        client,
        model,
        "agent_prompt_forced_search",
        agent_messages,
        TOOL_DEFINITIONS,
        {"type": "function", "function": {"name": "search_products"}},
    )
    await _request(
        client,
        model,
        "agent_prompt_with_user_message",
        [
            *agent_messages,
            {
                "role": "user",
                "content": "Select an eligible product using the fixed intent and available tools.",
            },
        ],
        TOOL_DEFINITIONS,
    )
    await _request(
        client,
        model,
        "agent_prompt_without_final_json",
        no_final_json_messages,
        TOOL_DEFINITIONS,
    )
    simplified_compare = {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": "Compare eligible product IDs.",
            "parameters": {
                "type": "object",
                "properties": {"product_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["product_ids"],
            },
        },
    }
    strict_compare = {
        **simplified_compare,
        "function": {
            **simplified_compare["function"],
            "parameters": {
                **simplified_compare["function"]["parameters"],
                "additionalProperties": False,
            },
        },
    }
    full_description_compare = {
        **simplified_compare,
        "function": {
            **simplified_compare["function"],
            "description": "Compare up to five eligible product IDs using safe structured fields.",
        },
    }
    await _request(
        client,
        model,
        "agent_prompt_simplified_compare",
        agent_messages,
        [*TOOL_DEFINITIONS[:2], simplified_compare],
    )
    await _request(
        client,
        model,
        "agent_prompt_strict_compare",
        agent_messages,
        [*TOOL_DEFINITIONS[:2], strict_compare],
    )
    await _request(
        client,
        model,
        "agent_prompt_full_description_compare",
        agent_messages,
        [*TOOL_DEFINITIONS[:2], full_description_compare],
    )
    first = await _request(client, model, "agent_turn_1", agent_messages, TOOL_DEFINITIONS)
    if first is None or not first.tool_calls:
        return
    assistant, results = _assistant_transcript(first)
    agent_messages.extend([assistant, *results])
    await _request(client, model, "agent_turn_2", agent_messages, TOOL_DEFINITIONS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a live, redacted Groq tool-call diagnostic.")
    parser.add_argument("--confirm-live", action="store_true", help="Permit authenticated Groq requests.")
    args = parser.parse_args()
    if not args.confirm_live:
        parser.error("Pass --confirm-live to make authenticated requests.")
    asyncio.run(main())
