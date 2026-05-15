from result import Result
from tools import TOOL_HANDLERS, CHILD_TOOLS
from runtime import get_runtime, print_usage


SUBAGENT_SYSTEM = (
    "You are a focused subagent. "
    "Complete the assigned task independently and return a concise summary. "
)

def extract_summary(content) -> str:
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        elif getattr(block, "type", None) == "text":
            parts.append(block.text)
    return ("\n".join(parts)) or "(no summary)"


def request_final_summary(runtime, sub_messages: list[dict]) -> str:
    response = runtime.call_with_retry(
        system=(
            SUBAGENT_SYSTEM
            + "The tool budget is exhausted. Use the available tool results and return the final concise summary now."
        ),
        messages=sub_messages,
        max_tokens=runtime.sub_max_tokens,
    )
    print_usage("subagent-final", response)
    return extract_summary(response.content)


def run_subagent(task: str) -> Result:
    runtime = get_runtime()
    
    sub_messages = [{"role": "user", "content": task}]
    
    for _ in range(0, runtime.sub_max_turns):
        response = runtime.call_with_retry(
            system=SUBAGENT_SYSTEM,
            messages=sub_messages,
            tools=CHILD_TOOLS,
            max_tokens=runtime.sub_max_tokens
        )
        sub_messages.append({"role": "assistant", "content": response.content})
        
        print_usage("subagent", response)
        if response.stop_reason != "tool_use":
            return Result(stdout=extract_summary(response.content))

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            print(f"$ AI use {tool_name}...")

            handler = TOOL_HANDLERS.get(tool_name)
            if handler is None:
                output = Result(1, stderr=f"Unknown tool: {tool_name}")
            else:
                try:
                    output = handler(**block.input)
                except TypeError as e:
                    output = Result(1, stderr=f"Invalid input for {tool_name}: {e}")
                except Exception as e:
                    output = Result(1, stderr=f"Tool {tool_name} failed: {e}")

            output_text = output.to_json()
            print(output_text)

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output_text,
                }
            )

        if not tool_results:
            return Result(stdout=extract_summary(response.content))

        sub_messages.append({"role": "user", "content": tool_results})
    
    try:
        return Result(stdout=request_final_summary(runtime, sub_messages))
    except Exception as error:
        return Result(1, stderr=f"Subagent failed to summarize after max turns: {error}")
    

        
        
    
