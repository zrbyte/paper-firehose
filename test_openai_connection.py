import os
from pathlib import Path
import openai


def load_api_key(path: str = "openaikulcs.env") -> str:
    """Load OPENAI_API_KEY from the given env file."""
    env_path = Path(path)
    if not env_path.is_file():
        raise FileNotFoundError(f"Environment file '{path}' not found")
    for line in env_path.read_text().splitlines():
        if line.strip().startswith("OPENAI_API_KEY"):
            key = line.split("=", 1)[1].strip()
            os.environ["OPENAI_API_KEY"] = key
            return key
    raise KeyError("OPENAI_API_KEY not found in env file")


def main() -> None:
    api_key = load_api_key()
    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": "Tell me a short joke."}],
        max_tokens=50,
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
