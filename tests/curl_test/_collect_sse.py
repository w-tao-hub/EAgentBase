"""SSE 流式响应收集工具。从 stdin 读取 SSE 事件流，提取 LLM 回复文本。"""
import sys
import json


def main() -> None:
    text_parts: list[str] = []
    for line in sys.stdin:
        line = line.strip()
        if line.startswith("data: "):
            data_str = line[6:]
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, ValueError):
                continue
            event_type = data.get("type", "")
            if event_type in ("message_delta",):
                content = data.get("content", "")
                text_parts.append(content)
            elif event_type == "run_completed":
                # 终态事件的 data 字段嵌套
                inner = data.get("data", {})
                content = inner.get("content", "")
                text_parts.append(content)
            elif event_type == "run_failed":
                print(f"RUN_FAILED: {data.get('error_code','')} {data.get('message','')}")
                sys.exit(1)
            elif event_type == "request_failed":
                print(f"REQUEST_FAILED: {data.get('error_code','')} {data.get('message','')}")
                sys.exit(1)
    print("".join(text_parts))


if __name__ == "__main__":
    main()
