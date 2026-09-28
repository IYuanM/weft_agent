import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from baseline.core.config import load_model_env
from baseline.models.chat import ChatModel, ModelError


class ConfigModelTests(unittest.TestCase):
    def test_local_config_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MODEL_NAME": "override"}, clear=True):
            path = Path(directory) / ".env"
            path.write_text('# comment\nMODEL_NAME=local\nMODEL_API_KEY="fixture"\n', encoding="utf-8")
            load_model_env(path)
            self.assertEqual(os.environ["MODEL_NAME"], "override")
            self.assertEqual(os.environ["MODEL_API_KEY"], "fixture")

    def test_bad_config_does_not_echo_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("BAD=private-value", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_model_env(path)
            self.assertNotIn("private-value", str(caught.exception))

    def test_request_and_response_contract(self):
        config = {"MODEL_NAME": "fixture-model", "MODEL_BASE_URL": "https://example.test/v1", "MODEL_API_KEY": "fixture"}
        body = {"choices": [{"message": {"role": "assistant", "content": "ok", "reasoning_content": "provider-field"},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 3}}
        with patch.dict(os.environ, config), patch("baseline.models.chat.urlopen", return_value=io.BytesIO(json.dumps(body).encode())) as request:
            reply = ChatModel().complete([{"role": "user", "content": "hello"}], [], 5)
            self.assertEqual(reply.message["reasoning_content"], "provider-field")
            self.assertEqual(reply.usage["total_tokens"], 3)
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, "https://example.test/v1/chat/completions")
            self.assertEqual(json.loads(sent.data)["model"], "fixture-model")
            self.assertEqual(request.call_args.kwargs["timeout"], 5)

    def test_http_error_is_safe(self):
        config = {"MODEL_NAME": "fixture", "MODEL_BASE_URL": "https://example.test", "MODEL_API_KEY": "fixture"}
        error = HTTPError("https://example.test", 401, "private-text", {}, io.BytesIO(b"private-response"))
        with patch.dict(os.environ, config), patch("baseline.models.chat.urlopen", side_effect=error):
            with self.assertRaises(ModelError) as caught:
                ChatModel().complete([], [], 5)
            self.assertEqual(caught.exception.http_status, 401)
            self.assertNotIn("private", str(caught.exception))
