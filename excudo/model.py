import threading
from collections.abc import Iterator

from openai import DefaultHttpxClient, OpenAI, Timeout

from . import config
from .messages import to_api
from .security import llm_endpoint_problem


def extra_field(obj, name: str):
    """Reads a field the openai SDK's pydantic models don't officially declare
    (e.g. vLLM's `reasoning`), whether it landed as an attribute or in
    model_extra."""
    value = getattr(obj, name, None)
    if value is not None:
        return value
    extra = getattr(obj, "model_extra", None)
    return extra.get(name) if extra else None


def llm_client(base_url: str, api_key: str, timeout, max_retries: int = 1) -> OpenAI:
    """OpenAI-compatible client for the LLM server. Redirects are never followed:
    a 30x answer could send the conversation to another host, so it fails as an
    error instead. Explicit timeouts; `max_retries` retries."""
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        http_client=DefaultHttpxClient(follow_redirects=False),
    )


class Model:
    """Thin wrapper around the LLM API.

    Knows how to ask the model for a completion given messages and tool
    schemas. Knows nothing about what the tools do, the filesystem, or the
    shell — that is the Harness's job.

    Excudo-only `metadata` fields are always stripped (`to_api`) before a
    request is sent.
    """

    def __init__(
        self,
        base_url: str = config.BASE_URL,
        api_key: str = config.API_KEY,
        model: str = config.MODEL_NAME,
    ):
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._client = self._new_client()
        # Set by cancel(); cleared by reset_cancel() when a turn starts.
        self._cancelled = threading.Event()

    def _new_client(self) -> OpenAI:
        """Create a client with explicit connection/read timeouts and one retry."""
        return llm_client(
            self._base_url,
            self._api_key,
            Timeout(config.LLM_READ_TIMEOUT, connect=config.LLM_CONNECT_TIMEOUT),
        )

    def _check_endpoint(self) -> None:
        """Refuses to send anything to a server address the endpoint policy rejects."""
        problem = llm_endpoint_problem(self._base_url)
        if problem:
            raise ValueError(f"LLM server refused: {problem}")

    def generate_response(self, messages: list[dict], timeout: float = 120) -> str:
        """Plain text in, plain text out: no tools, no streaming. Used for
        auxiliary tasks such as summarizing old history. Returns "" if the
        model produced no text."""
        self._check_endpoint()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=to_api(messages),
            timeout=timeout,
        )
        return response.choices[0].message.content or ""

    def complete(self, messages: list[dict], tools: list[dict]):
        self._check_endpoint()
        response = self._client.chat.completions.create(
            model=self._model,
            messages=to_api(messages),
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
        )
        return response.choices[0].message

    def stream(self, messages: list[dict], tools: list[dict], *, thinking: bool | None = None) -> Iterator:
        """Yields raw chunk deltas as the model generates them, for live
        token-by-token rendering."""
        self._check_endpoint()
        if self._cancelled.is_set():
            raise RuntimeError("Stopped by the user.")
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=to_api(messages),
            **({"tools": tools, "tool_choice": "auto"} if tools else {}),
            stream=True,
            # Noneなら従来と同じリクエスト。挨拶や高速モードではFalseを明示する。
            **({"extra_body": {"chat_template_kwargs": {"enable_thinking": thinking}}}
               if thinking is not None else {}),
        )
        if self._cancelled.is_set():  # Stop was pressed while the request was being made
            close = getattr(stream, "close", None)
            if close is not None:
                close()
            raise RuntimeError("Stopped by the user.")
        return stream

    def cancel(self) -> None:
        """UI側ではフラグだけを立て、通信資源には触れない。

        Mac/Linuxでは他スレッドのcloseでrecvが起きず、サーバーだけが送信を
        止めることがある。Harnessが次のチャンクで停止を検知し、ワーカー側の
        finallyでストリームを閉じる。初回応答前・通信停止中はタイムアウト待ち。
        """
        self._cancelled.set()

    def reset_cancel(self) -> None:
        """Clears the future desktop Stop button's cancellation state. Called by
        Harness.run() on the agent worker thread when a new turn starts."""
        self._cancelled.clear()
