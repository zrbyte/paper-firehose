import json
import os
import urllib.request


def load_api_key(path="openaikulcs.env"):
    """Read the OpenAI API key from a file."""
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(__file__), path)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def main():
    api_key = load_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # List available models
    req = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers=headers,
    )
    with urllib.request.urlopen(req) as response:
        models_data = json.load(response)

    for model in models_data.get("data", []):
        print(model["id"])

    # Chat completion
    data = json.dumps({
        "model": "gpt-4.1-nano",
        "messages": [{"role": "user", "content": "Tell me a short joke."}],
        "max_tokens": 50,
    }).encode("utf-8")

    chat_req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        data=data,
    )
    with urllib.request.urlopen(chat_req) as chat_response:
        chat_data = json.load(chat_response)

    print(chat_data["choices"][0]["message"]["content"])


if __name__ == "__main__":
    main()
