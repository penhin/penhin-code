import os

from pathlib import Path
from skills import load_skill
from tools.registry import tool_description_lines


USER_PROMPT_PATH = Path("AGENTS.md")


def append_project_instructions(system: str) -> str:
    if not USER_PROMPT_PATH.exists():
        return system
    return system + "\n\n# Project Instructions\n\n" + USER_PROMPT_PATH.read_text(encoding="utf-8")


def build_main_system() -> str:
    return append_project_instructions(MAIN_SYSTEM)


def build_subagent_system() -> str:
    return append_project_instructions(SUBAGENT_SYSTEM)


def build_subagent_final_system() -> str:
    return append_project_instructions(SUBAGENT_SYSTEM) + (
        "\n\n"
        "The tool budget is exhausted. Use the available tool results and return the final concise summary now."
    )


TASK_WORKFLOW = (
    "\n\nTask workflow:\n"
    "- For non-trivial user requests, start or update the tracked task before making changes.\n"
    "- Use task_start only when beginning a new user-level task, with a short subject and a 2-5 item plan.\n"
    "- Treat task_start(plan=[...]) as the primary way to create the initial todo list.\n"
    "- Do not use task_start for internal workflow steps, retries, status checks, or tool failures.\n"
    "- Use todo_done as steps are completed, and keep todos focused on executable steps.\n"
    "- If a tool is rejected and progress cannot continue, use task_block with a short note instead of starting a new task.\n"
    "- Use task_complete when the requested work is done, or task_block when progress is blocked.\n"
    "- Do not create tasks or todos for simple questions, tiny lookups, or one-step responses."
)


MAIN_SYSTEM = (
    f"You are Penhin Code, a tiny coding agent running in {os.getcwd()}. "
    "Use the available tools according to their descriptions. "
    "Prefer structured tools over ad hoc shell commands for file operations. "
    "Tool results are JSON with ok/message/data/error/meta fields; prefer data for structured facts and error for failures. "
    "Ignore .venv, .git, __pycache__, skills, and internal state files."
    f"{TASK_WORKFLOW}"
    "\n\nAvailable tools:\n"
    f"{chr(10).join(tool_description_lines())}"
    "\n\nAvailable skills:\n"
    f"{load_skill.get_descriptions()}"
)


SUBAGENT_SYSTEM = (
    "You are a focused subagent. "
    "Complete the assigned task independently and return a concise summary. "
    "Follow project instructions, but keep the assigned task narrow and do not expand scope. "
    "Tool results are JSON with ok/message/data/error/meta fields; prefer data for structured facts and error for failures. "
)


AUTO_COMPACT_SYSTEM = (
    "You summarize coding-agent conversation history into a compact "
    "state snapshot for continuing engineering work."
)
