#!/usr/bin/env python3
"""Generate a Siri Plus .shortcut file for webui-siri-shortcut.

Produces an Apple Shortcuts plist (XML format) that can be imported directly
on macOS by double-clicking, or converted to binary plist for iOS:

    plutil -convert binary1 siri-plus.shortcut

Usage:
    python generate_shortcut.py --url https://YOUR_SERVER --api-key YOUR_KEY
    python generate_shortcut.py --url https://YOUR_SERVER --api-key YOUR_KEY \\
        --output my-shortcut.shortcut

No external dependencies required — uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import plistlib
import sys
import uuid
from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: text token string (plain text with no variable interpolation)
# ---------------------------------------------------------------------------

def _text_token(value: str) -> dict:
    """Encode a plain string as a WFTextTokenString."""
    return {
        "Value": {
            "attachmentsByRange": {},
            "string": value,
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _variable_token(output_uuid: str, output_name: str) -> dict:
    """Encode a reference to a previous action's output variable."""
    return {
        "Value": {
            "attachmentsByRange": {
                "{0, 1}": {
                    "OutputName": output_name,
                    "OutputUUID": output_uuid,
                    "Type": "ActionOutput",
                },
            },
            "string": "\ufffc",
        },
        "WFSerializationType": "WFTextTokenString",
    }


def _concat_tokens(*parts) -> dict:
    """Encode a string that concatenates multiple plain text and variable parts.

    Each part is either a plain str or a (output_uuid, output_name) tuple.
    """
    attachments = {}
    result = ""
    for part in parts:
        if isinstance(part, str):
            result += part
        else:
            output_uuid, output_name = part
            pos = len(result)
            attachments[f"{{{pos}, 1}}"] = {
                "OutputName": output_name,
                "OutputUUID": output_uuid,
                "Type": "ActionOutput",
            }
            result += "\ufffc"
    return {
        "Value": {
            "attachmentsByRange": attachments,
            "string": result,
        },
        "WFSerializationType": "WFTextTokenString",
    }


# ---------------------------------------------------------------------------
# Action builders
# ---------------------------------------------------------------------------

def _speak_action(text_token: dict, uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.speaktext",
        "WFWorkflowActionParameters": {
            "CustomOutputName": "Spoken Text",
            "UUID": uuid_str,
            "WFSpeakTextLanguage": "",
            "WFSpeakTextPitch": 1.0,
            "WFSpeakTextRate": 0.5,
            "WFSpeakTextWaitUntilFinished": True,
            "WFText": text_token,
        },
    }


def _dictate_action(output_name: str, uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.dictatetext",
        "WFWorkflowActionParameters": {
            "CustomOutputName": output_name,
            "UUID": uuid_str,
            "WFDictateTextLanguage": "",
            "WFDictateTextStopListening": "AfterPause",
        },
    }


def _set_variable_action(name: str, input_uuid: str, input_name: str, uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.setvariable",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
            "WFInput": _variable_token(input_uuid, input_name),
            "WFVariableName": name,
        },
    }


def _get_variable_action(name: str, output_name: str, uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getvariable",
        "WFWorkflowActionParameters": {
            "CustomOutputName": output_name,
            "UUID": uuid_str,
            "WFVariable": {
                "Value": {
                    "VariableName": name,
                    "Type": "Variable",
                },
                "WFSerializationType": "WFTextTokenAttachment",
            },
        },
    }


def _url_request_action(
    url_token: dict,
    method: str,
    headers: dict[str, str],
    json_body: dict[str, dict],  # key → WFTextTokenString token
    output_name: str,
    uuid_str: str,
) -> dict:
    header_items = [
        {
            "WFItemType": 0,
            "WFKey": _text_token(k),
            "WFValue": _text_token(v),
        }
        for k, v in headers.items()
    ]
    body_items = [
        {
            "WFItemType": 0,
            "WFKey": _text_token(k),
            "WFValue": v,
        }
        for k, v in json_body.items()
    ]
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
        "WFWorkflowActionParameters": {
            "CustomOutputName": output_name,
            "UUID": uuid_str,
            "WFURL": url_token,
            "WFHTTPMethod": method,
            "WFHTTPBodyType": "JSON",
            "WFHTTPRequestHeaders": {
                "Value": {
                    "WFDictionaryFieldValueItems": header_items,
                },
                "WFSerializationType": "WFDictionaryFieldValue",
            },
            "WFHTTPRequestJSON": {
                "Value": {
                    "WFDictionaryFieldValueItems": body_items,
                },
                "WFSerializationType": "WFDictionaryFieldValue",
            },
            "WFHTTPAllowCachePolicy": False,
        },
    }


def _get_dict_value_action(
    key: str,
    dict_input_uuid: str,
    dict_input_name: str,
    output_name: str,
    uuid_str: str,
) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.getvalueforkey",
        "WFWorkflowActionParameters": {
            "CustomOutputName": output_name,
            "UUID": uuid_str,
            "WFDictionaryKey": _text_token(key),
            "WFInput": _variable_token(dict_input_uuid, dict_input_name),
        },
    }


def _repeat_action(count: int, uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.repeat.count",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
            "WFRepeatCount": count,
        },
    }


def _end_repeat_action(uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.endrepeat",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
        },
    }


def _if_action(
    input_uuid: str,
    input_name: str,
    condition: int,  # 99 = contains
    value: str,
    uuid_str: str,
    group_uuid: str,
) -> dict:
    """WFCondition 99 = contains (case-insensitive in Shortcuts)."""
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
            "GroupingIdentifier": group_uuid,
            "WFCondition": condition,
            "WFConditionalActionString": value,
            "WFControlFlowMode": 0,
            "WFInput": {
                "Value": {
                    "OutputName": input_name,
                    "OutputUUID": input_uuid,
                    "Type": "ActionOutput",
                },
                "WFSerializationType": "WFTextTokenAttachment",
            },
        },
    }


def _otherwise_action(uuid_str: str, group_uuid: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
            "GroupingIdentifier": group_uuid,
            "WFControlFlowMode": 1,
        },
    }


def _end_if_action(uuid_str: str, group_uuid: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.conditional",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
            "GroupingIdentifier": group_uuid,
            "WFControlFlowMode": 2,
        },
    }


def _stop_action(uuid_str: str) -> dict:
    return {
        "WFWorkflowActionIdentifier": "is.workflow.actions.exit",
        "WFWorkflowActionParameters": {
            "UUID": uuid_str,
        },
    }


# ---------------------------------------------------------------------------
# Shortcut builder
# ---------------------------------------------------------------------------

def build_shortcut(server_url: str, api_key: str) -> dict:
    """Build the complete Shortcuts plist dict."""

    # Remove trailing slash from server URL
    server_url = server_url.rstrip("/")

    # UUIDs for each action (deterministic names for readability)
    u = {k: str(uuid.uuid4()).upper() for k in [
        "speak_yes",
        "dictate_question",
        "http_new_chat",
        "get_response",
        "get_chat_id",
        "speak_first_response",
        "repeat",
        "dictate_followup",
        "if_no",
        "speak_bye",
        "stop",
        "otherwise",
        "http_followup",
        "get_followup_response",
        "speak_followup_response",
        "end_if",
        "end_repeat",
    ]}

    # Shared group UUID for the if/otherwise/end-if block
    if_group = str(uuid.uuid4()).upper()

    actions = [
        # 1. Speak "Yes?"
        _speak_action(_text_token("Yes?"), u["speak_yes"]),

        # 2. Dictate initial question
        _dictate_action("Question", u["dictate_question"]),

        # 3. POST /api/chat with the question
        _url_request_action(
            url_token=_text_token(f"{server_url}/api/chat"),
            method="POST",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json_body={
                "message": _variable_token(u["dictate_question"], "Question"),
            },
            output_name="ChatResponse",
            uuid_str=u["http_new_chat"],
        ),

        # 4. Extract "response" from ChatResponse
        _get_dict_value_action(
            key="response",
            dict_input_uuid=u["http_new_chat"],
            dict_input_name="ChatResponse",
            output_name="AssistantReply",
            uuid_str=u["get_response"],
        ),

        # 5. Extract "chat_id" from ChatResponse
        _get_dict_value_action(
            key="chat_id",
            dict_input_uuid=u["http_new_chat"],
            dict_input_name="ChatResponse",
            output_name="ChatID",
            uuid_str=u["get_chat_id"],
        ),

        # 6. Speak the first response
        _speak_action(
            _variable_token(u["get_response"], "AssistantReply"),
            u["speak_first_response"],
        ),

        # 7. Start repeat loop (9999 = effectively infinite)
        _repeat_action(9999, u["repeat"]),

        #   7a. Dictate follow-up
        _dictate_action("FollowUp", u["dictate_followup"]),

        #   7b. If FollowUp contains "no" → exit
        _if_action(
            input_uuid=u["dictate_followup"],
            input_name="FollowUp",
            condition=99,  # contains
            value="no",
            uuid_str=u["if_no"],
            group_uuid=if_group,
        ),

        #     7c. Speak goodbye
        _speak_action(_text_token("OK, see you"), u["speak_bye"]),

        #     7d. Stop shortcut
        _stop_action(u["stop"]),

        #   7e. Otherwise
        _otherwise_action(u["otherwise"], if_group),

        #   7f. POST /api/chat/{ChatID}/message
        _url_request_action(
            url_token=_concat_tokens(
                f"{server_url}/api/chat/",
                (u["get_chat_id"], "ChatID"),
                "/message",
            ),
            method="POST",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json_body={
                "message": _variable_token(u["dictate_followup"], "FollowUp"),
            },
            output_name="FollowUpResponse",
            uuid_str=u["http_followup"],
        ),

        #   7g. Extract "response" from FollowUpResponse
        _get_dict_value_action(
            key="response",
            dict_input_uuid=u["http_followup"],
            dict_input_name="FollowUpResponse",
            output_name="Reply",
            uuid_str=u["get_followup_response"],
        ),

        #   7h. Speak follow-up response
        _speak_action(
            _variable_token(u["get_followup_response"], "Reply"),
            u["speak_followup_response"],
        ),

        #   7i. End If
        _end_if_action(u["end_if"], if_group),

        # 8. End Repeat
        _end_repeat_action(u["end_repeat"]),
    ]

    return {
        "WFWorkflowClientVersion": "1400.0.0",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowIcon": {
            "WFWorkflowIconGlyphNumber": 59511,  # microphone glyph
            "WFWorkflowIconStartColor": 431817727,  # teal
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowActions": actions,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a Siri Plus .shortcut file for webui-siri-shortcut.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python generate_shortcut.py --url https://siri.example.com --api-key abc123
  python generate_shortcut.py --url https://siri.example.com --api-key abc123 \\
      --output my-siri-plus.shortcut

After generating:
  - macOS: double-click the .shortcut file to import
  - If that fails: plutil -convert binary1 siri-plus.shortcut
  - iOS: AirDrop the file to your device, then tap to import
        """,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="Base URL of your webui-siri-shortcut server (e.g. https://siri.example.com)",
    )
    parser.add_argument(
        "--api-key",
        required=True,
        help="API key configured in the API_KEY environment variable",
    )
    parser.add_argument(
        "--output",
        default="siri-plus.shortcut",
        help="Output file path (default: siri-plus.shortcut)",
    )
    args = parser.parse_args()

    data = build_shortcut(server_url=args.url, api_key=args.api_key)
    out = Path(args.output)

    with out.open("wb") as f:
        plistlib.dump(data, f, fmt=plistlib.FMT_XML)

    print(f"Shortcut written to: {out}")
    print()
    print("Import options:")
    print(f"  macOS: double-click {out}")
    print(f"  If import fails, convert first: plutil -convert binary1 {out}")
    print(f"  iOS: AirDrop {out} to your device, then tap to import")


if __name__ == "__main__":
    main()
