import os
import smtplib
import sys
import tempfile
import traceback
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyring.errors

import kindle_delivery as kd
import md_to_kindle

DUMMY_PASSWORD = "s3cr3t-app-password"
SENTINEL = "SENTINEL-pw-7f3a9c"


class ShouldSendToKindleTests(unittest.TestCase):
    def test_default_epub_sends(self):
        self.assertTrue(kd.should_send_to_kindle(None, "epub"))

    def test_explicit_false_epub_does_not_send(self):
        self.assertFalse(kd.should_send_to_kindle(False, "epub"))

    def test_explicit_true_epub_sends(self):
        self.assertTrue(kd.should_send_to_kindle(True, "epub"))

    def test_default_pdf_does_not_send(self):
        self.assertFalse(kd.should_send_to_kindle(None, "pdf"))

    def test_explicit_true_pdf_still_does_not_send(self):
        self.assertFalse(kd.should_send_to_kindle(True, "pdf"))

    def test_explicit_false_pdf_does_not_send(self):
        self.assertFalse(kd.should_send_to_kindle(False, "pdf"))


class ConfigTests(unittest.TestCase):
    def test_get_sender_email_missing_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_SENDER_EMAIL, None)
            with self.assertRaises(kd.MissingConfigError):
                kd.get_sender_email()

    def test_get_sender_email_from_env(self):
        with patch.dict(os.environ, {kd.ENV_SENDER_EMAIL: "sender@example.com"}):
            self.assertEqual(kd.get_sender_email(), "sender@example.com")

    def test_get_dest_email_missing_raises(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_DEST_EMAIL, None)
            with self.assertRaises(kd.MissingConfigError):
                kd.get_dest_email()

    def test_get_dest_email_from_env(self):
        with patch.dict(os.environ, {kd.ENV_DEST_EMAIL: "dest@kindle.com"}):
            self.assertEqual(kd.get_dest_email(), "dest@kindle.com")


class CredentialTests(unittest.TestCase):
    def test_get_app_password_from_keyring(self):
        with patch.object(kd.keyring, "get_password", return_value=DUMMY_PASSWORD):
            self.assertEqual(kd.get_app_password("user@example.com"), DUMMY_PASSWORD)

    def test_missing_credential_raises(self):
        with patch.object(kd.keyring, "get_password", return_value=None), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_APP_PASSWORD, None)
            with self.assertRaises(kd.MissingCredentialError) as ctx:
                kd.get_app_password("user@example.com")
            self.assertNotIn(DUMMY_PASSWORD, str(ctx.exception))

    def test_env_fallback_used_when_keyring_empty(self):
        with patch.object(kd.keyring, "get_password", return_value=None), \
             patch.dict(os.environ, {kd.ENV_APP_PASSWORD: DUMMY_PASSWORD}):
            self.assertEqual(kd.get_app_password("user@example.com"), DUMMY_PASSWORD)

    def test_backend_error_falls_back_to_env(self):
        with patch.object(kd.keyring, "get_password",
                           side_effect=keyring.errors.KeyringError("no backend")), \
             patch.dict(os.environ, {kd.ENV_APP_PASSWORD: DUMMY_PASSWORD}):
            self.assertEqual(kd.get_app_password("user@example.com"), DUMMY_PASSWORD)

    def test_backend_error_without_env_raises_keyring_backend_error(self):
        with patch.object(kd.keyring, "get_password",
                           side_effect=keyring.errors.KeyringError("no backend")), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_APP_PASSWORD, None)
            with self.assertRaises(kd.KeyringBackendError) as ctx:
                kd.get_app_password("user@example.com")
            self.assertNotIn(DUMMY_PASSWORD, str(ctx.exception))

    def test_set_app_password_wraps_backend_error(self):
        with patch.object(kd.keyring, "set_password",
                           side_effect=keyring.errors.KeyringError("nope")):
            with self.assertRaises(kd.KeyringBackendError):
                kd.set_app_password(DUMMY_PASSWORD, "user@example.com")

    def test_clear_app_password_noop_when_nothing_stored(self):
        with patch.object(kd.keyring, "delete_password",
                           side_effect=keyring.errors.PasswordDeleteError("nothing")):
            kd.clear_app_password("user@example.com")  # should not raise

    def test_clear_app_password_wraps_other_backend_errors(self):
        with patch.object(kd.keyring, "delete_password",
                           side_effect=keyring.errors.KeyringError("broken")):
            with self.assertRaises(kd.KeyringBackendError):
                kd.clear_app_password("user@example.com")


class SendToKindleTests(unittest.TestCase):
    def setUp(self):
        self.tmp_path = "/tmp/_kindle_delivery_test.epub"
        with open(self.tmp_path, "wb") as f:
            f.write(b"fake epub bytes")
        self.addCleanup(lambda: os.path.exists(self.tmp_path) and os.remove(self.tmp_path))

    def test_missing_file_raises(self):
        with self.assertRaises(kd.MissingFileError):
            kd.send_to_kindle("/tmp/_does_not_exist.epub")

    def test_unsupported_extension_raises(self):
        bad_path = "/tmp/_kindle_delivery_test.txt"
        with open(bad_path, "w") as f:
            f.write("hi")
        self.addCleanup(lambda: os.remove(bad_path))
        with patch.object(kd, "get_app_password", return_value=DUMMY_PASSWORD):
            with self.assertRaises(kd.UnsupportedFormatError):
                kd.send_to_kindle(bad_path)

    def test_successful_send(self):
        mock_server = MagicMock()
        mock_smtp_ssl = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        with patch.object(kd, "get_app_password", return_value=DUMMY_PASSWORD), \
             patch.object(kd.smtplib, "SMTP_SSL", mock_smtp_ssl):
            kd.send_to_kindle(self.tmp_path, sender_email="sender@example.com",
                               dest_email="dest@kindle.com")
        mock_server.login.assert_called_once_with("sender@example.com", DUMMY_PASSWORD)
        self.assertEqual(mock_server.send_message.call_count, 1)
        sent_msg = mock_server.send_message.call_args[0][0]
        attachment = list(sent_msg.iter_attachments())[0]
        self.assertEqual(attachment.get_filename(), os.path.basename(self.tmp_path))
        self.assertEqual(attachment.get_payload(decode=True), b"fake epub bytes")

    def test_smtp_auth_failure(self):
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad creds")
        mock_smtp_ssl = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        with patch.object(kd, "get_app_password", return_value=DUMMY_PASSWORD), \
             patch.object(kd.smtplib, "SMTP_SSL", mock_smtp_ssl):
            with self.assertRaises(kd.SmtpAuthError) as ctx:
                kd.send_to_kindle(self.tmp_path, sender_email="sender@example.com",
                                   dest_email="dest@kindle.com")
            self.assertNotIn(DUMMY_PASSWORD, str(ctx.exception))

    def test_smtp_connection_failure(self):
        mock_smtp_ssl = MagicMock(side_effect=TimeoutError("timed out"))
        with patch.object(kd, "get_app_password", return_value=DUMMY_PASSWORD), \
             patch.object(kd.smtplib, "SMTP_SSL", mock_smtp_ssl):
            with self.assertRaises(kd.SmtpConnectionError) as ctx:
                kd.send_to_kindle(self.tmp_path, sender_email="sender@example.com",
                                   dest_email="dest@kindle.com")
            self.assertNotIn(DUMMY_PASSWORD, str(ctx.exception))


class CliParserTests(unittest.TestCase):
    def test_default_send_to_kindle_is_none(self):
        args = md_to_kindle.build_parser().parse_args(["input.md"])
        self.assertIsNone(args.send_to_kindle)

    def test_explicit_no_send_to_kindle(self):
        args = md_to_kindle.build_parser().parse_args(["input.md", "--no-send-to-kindle"])
        self.assertFalse(args.send_to_kindle)

    def test_explicit_send_to_kindle(self):
        args = md_to_kindle.build_parser().parse_args(["input.md", "--send-to-kindle"])
        self.assertTrue(args.send_to_kindle)

    def test_set_kindle_password_flag(self):
        args = md_to_kindle.build_parser().parse_args(["--set-kindle-password"])
        self.assertTrue(args.set_kindle_password)

    def test_clear_kindle_password_flag(self):
        args = md_to_kindle.build_parser().parse_args(["--clear-kindle-password"])
        self.assertTrue(args.clear_kindle_password)


class ConversionFailureMeansNoEmailTests(unittest.TestCase):
    def test_send_not_called_when_convert_raises(self):
        input_path = "/tmp/_kindle_delivery_fake_input.md"
        with open(input_path, "w") as f:
            f.write("# hi\n")
        self.addCleanup(lambda: os.remove(input_path))
        output_path = "/tmp/_should_not_be_created.epub"

        with patch.object(md_to_kindle, "convert", side_effect=ValueError("boom")), \
             patch.object(kd, "send_to_kindle") as mock_send, \
             patch.object(sys, "argv", ["md_to_kindle.py", input_path, output_path]):
            with self.assertRaises(ValueError):
                md_to_kindle.main()
            mock_send.assert_not_called()


class NoPasswordLeakTests(unittest.TestCase):
    """The App Password must never appear in a raised delivery error's
    message, its (chained) traceback, or a locals-capturing traceback.

    Exceptions are captured with an explicit try/except rather than
    assertRaises: assertRaises calls traceback.clear_frames(), which wipes
    frame locals and would make the captured-locals check vacuous.
    """

    def setUp(self):
        fd, self.tmp_path = tempfile.mkstemp(suffix=".epub")
        os.write(fd, b"fake epub bytes")
        os.close(fd)
        self.addCleanup(os.remove, self.tmp_path)

    def _capture(self, fn, expected_type):
        try:
            fn()
        except BaseException as exc:  # keep exc.__traceback__ frames intact
            self.assertIsInstance(exc, expected_type)
            return exc
        self.fail(f"{expected_type.__name__} not raised")

    def _assert_no_leak(self, exc):
        self.assertNotIn(SENTINEL, str(exc))
        self.assertNotIn(SENTINEL, repr(exc))
        self.assertNotIn(SENTINEL, "".join(traceback.format_exception(exc)))
        with_locals = "".join(traceback.TracebackException.from_exception(
            exc, capture_locals=True).format())
        self.assertNotIn(SENTINEL, with_locals)
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)

    def _send(self, mock_smtp_ssl):
        with patch.object(kd, "get_app_password", return_value=SENTINEL), \
             patch.object(kd.smtplib, "SMTP_SSL", mock_smtp_ssl):
            kd.send_to_kindle(self.tmp_path, sender_email="sender@example.com",
                               dest_email="dest@kindle.com")

    def _smtp_with_server(self, mock_server):
        mock_smtp_ssl = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_server
        return mock_smtp_ssl

    def test_smtp_auth_error_never_leaks_password(self):
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(
            535, f"bad {SENTINEL}".encode())
        exc = self._capture(lambda: self._send(self._smtp_with_server(mock_server)),
                            kd.SmtpAuthError)
        self._assert_no_leak(exc)
        self.assertIn("SMTP 535", str(exc))

    def test_smtp_other_error_never_leaks_password(self):
        mock_server = MagicMock()
        mock_server.send_message.side_effect = smtplib.SMTPDataError(550, SENTINEL.encode())
        exc = self._capture(lambda: self._send(self._smtp_with_server(mock_server)),
                            kd.SmtpConnectionError)
        self._assert_no_leak(exc)
        self.assertIn("SMTPDataError", str(exc))
        self.assertIn("550", str(exc))

    def test_non_integer_smtp_code_not_echoed(self):
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(
            SENTINEL.encode(), b"x")
        exc = self._capture(lambda: self._send(self._smtp_with_server(mock_server)),
                            kd.SmtpAuthError)
        self._assert_no_leak(exc)

    def test_oserror_strerror_not_echoed(self):
        mock_smtp_ssl = MagicMock(side_effect=OSError(5, SENTINEL))
        exc = self._capture(lambda: self._send(mock_smtp_ssl), kd.SmtpConnectionError)
        self._assert_no_leak(exc)
        self.assertIn("OSError", str(exc))
        self.assertIn("errno 5", str(exc))

    def test_oserror_reports_class_name(self):
        mock_smtp_ssl = MagicMock(side_effect=TimeoutError("timed out"))
        exc = self._capture(lambda: self._send(mock_smtp_ssl), kd.SmtpConnectionError)
        self._assert_no_leak(exc)
        self.assertIn("TimeoutError", str(exc))

    def test_message_build_failure_before_smtp_never_leaks_password(self):
        with patch.object(kd, "EmailMessage", side_effect=RuntimeError("boom")):
            exc = self._capture(lambda: self._send(MagicMock()), RuntimeError)
        with_locals = "".join(traceback.TracebackException.from_exception(
            exc, capture_locals=True).format())
        self.assertNotIn(SENTINEL, with_locals)

    def test_set_app_password_never_leaks_password(self):
        with patch.object(kd.keyring, "set_password",
                           side_effect=keyring.errors.KeyringError(f"failed {SENTINEL}")):
            exc = self._capture(lambda: kd.set_app_password(SENTINEL, "user@example.com"),
                                kd.KeyringBackendError)
        self._assert_no_leak(exc)
        self.assertIn("KeyringError", str(exc))

    def test_set_app_password_missing_config_never_leaks_password(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_SENDER_EMAIL, None)
            exc = self._capture(lambda: kd.set_app_password(SENTINEL),
                                kd.MissingConfigError)
        self._assert_no_leak(exc)

    def test_get_app_password_backend_error_not_echoed(self):
        with patch.object(kd.keyring, "get_password",
                           side_effect=keyring.errors.KeyringError(SENTINEL)), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(kd.ENV_APP_PASSWORD, None)
            exc = self._capture(lambda: kd.get_app_password("user@example.com"),
                                kd.KeyringBackendError)
        self._assert_no_leak(exc)
        self.assertIn("KeyringError", str(exc))

    def test_clear_app_password_backend_error_not_echoed(self):
        with patch.object(kd.keyring, "delete_password",
                           side_effect=keyring.errors.KeyringError(SENTINEL)):
            exc = self._capture(lambda: kd.clear_app_password("user@example.com"),
                                kd.KeyringBackendError)
        self._assert_no_leak(exc)
        self.assertIn("KeyringError", str(exc))


class SetKindlePasswordCliNoLeakTests(unittest.TestCase):
    def test_store_failure_exit_never_leaks_password(self):
        # Explicit try/except (not assertRaises, which clears frame locals).
        # The real set_app_password runs (only the keyring backend is
        # mocked), so the SystemExit's __context__ chain is the genuine one.
        exc = None
        with patch("getpass.getpass", return_value=SENTINEL), \
             patch.dict(os.environ, {kd.ENV_SENDER_EMAIL: "sender@example.com"}), \
             patch.object(kd.keyring, "set_password",
                          side_effect=keyring.errors.KeyringError("store failed")), \
             patch.object(sys, "argv", ["md_to_kindle.py", "--set-kindle-password"]):
            try:
                md_to_kindle.main()
            except SystemExit as e:
                exc = e
        self.assertIsNotNone(exc, "SystemExit not raised")
        self.assertEqual(exc.code, 1)
        with_locals = "".join(traceback.TracebackException.from_exception(
            exc, capture_locals=True).format())
        self.assertNotIn(SENTINEL, with_locals)


class CliNoOutputPathFormatTests(unittest.TestCase):
    """main() with no OUTPUT must honour --output-format (not the --paste
    --format flag) and auto-send EPUB output. Delivery is mocked."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmpdir, ignore_errors=True))
        self.input_path = os.path.join(self.tmpdir, "x.md")
        with open(self.input_path, "w") as f:
            f.write("# hi\n")

    def _run_main(self, *extra):
        with patch.object(md_to_kindle, "OUTPUT_DIR", self.tmpdir), \
             patch.object(md_to_kindle, "convert") as mock_convert, \
             patch.object(kd, "send_to_kindle") as mock_send, \
             patch.object(kd, "get_dest_email", return_value="dest@kindle.com"), \
             patch.object(sys, "argv", ["md_to_kindle.py", self.input_path, *extra]):
            md_to_kindle.main()
        out_path = mock_convert.call_args.args[1]
        return mock_convert.call_args.kwargs["output_format"], out_path, mock_send

    def test_no_output_path_defaults_to_epub_and_sends(self):
        fmt, out_path, mock_send = self._run_main()
        self.assertEqual(fmt, "epub")
        self.assertTrue(out_path.endswith(".epub"))
        mock_send.assert_called_once_with(out_path)

    def test_no_output_path_output_format_pdf(self):
        fmt, out_path, mock_send = self._run_main("--output-format", "pdf")
        self.assertEqual(fmt, "pdf")
        self.assertTrue(out_path.endswith(".pdf"))
        mock_send.assert_not_called()

    def test_no_output_path_ignores_paste_format_flag(self):
        fmt, out_path, mock_send = self._run_main("--format", "html")
        self.assertEqual(fmt, "epub")
        self.assertTrue(out_path.endswith(".epub"))
        mock_send.assert_called_once_with(out_path)

    def test_no_output_path_no_send_flag(self):
        fmt, out_path, mock_send = self._run_main("--no-send-to-kindle")
        self.assertEqual(fmt, "epub")
        mock_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
