import os
import smtplib
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import keyring.errors

import kindle_delivery as kd
import md_to_kindle

DUMMY_PASSWORD = "s3cr3t-app-password"


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


if __name__ == "__main__":
    unittest.main()
