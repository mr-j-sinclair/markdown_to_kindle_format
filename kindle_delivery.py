"""Send-to-Kindle delivery: emails a generated EPUB to a Kindle address via
Amazon's "Send to Kindle by email" mechanism, over Gmail SMTP.

Kept fully separate from the conversion pipeline in md_to_kindle.py -- the
only thing this module needs from a conversion is a finished file on disk.

Credential handling: the Gmail App Password is never hardcoded, logged, or
passed on the command line. It's read from the OS keyring (macOS Keychain /
Windows Credential Locker, via the `keyring` package), with an environment
variable as a fallback for CI/headless use. See get_app_password().

Configuration: neither the sender Gmail address nor the destination Kindle
address is hardcoded here either -- this tool may be used by someone else,
or the addresses may change, so both are required environment variables
(see get_sender_email()/get_dest_email()) rather than source defaults.

Delivery contract: a successful send means Gmail accepted the email for
delivery -- it does NOT mean Amazon has confirmed the document landed in
the Kindle library. That confirmation (or rejection) is a separate,
asynchronous step on Amazon's side that this module cannot observe.
"""
import os
import smtplib
from email.message import EmailMessage

import keyring
import keyring.errors

# ---------- Configuration ----------
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT = 30

KEYRING_SERVICE = "markdown_to_kindle_format"
ENV_SENDER_EMAIL = "MD_TO_KINDLE_SENDER_EMAIL"
ENV_DEST_EMAIL = "MD_TO_KINDLE_DEST_EMAIL"
ENV_APP_PASSWORD = "MD_TO_KINDLE_APP_PASSWORD"  # fallback only; keyring is preferred

_ATTACHMENT_MIME = {
    ".epub": ("application", "epub+zip"),
    ".pdf": ("application", "pdf"),
}


class KindleDeliveryError(Exception):
    """Base class for all Send-to-Kindle delivery failures."""


class MissingConfigError(KindleDeliveryError):
    pass


class MissingCredentialError(KindleDeliveryError):
    pass


class KeyringBackendError(KindleDeliveryError):
    pass


class SmtpAuthError(KindleDeliveryError):
    pass


class SmtpConnectionError(KindleDeliveryError):
    pass


class MissingFileError(KindleDeliveryError):
    pass


class UnsupportedFormatError(KindleDeliveryError):
    pass


def get_sender_email():
    """Return the configured sender Gmail address, or raise MissingConfigError."""
    email = os.environ.get(ENV_SENDER_EMAIL)
    if not email:
        raise MissingConfigError(
            f"{ENV_SENDER_EMAIL} is not set -- set it to the dedicated "
            f"Gmail sender address before using Send-to-Kindle delivery"
        )
    return email


def get_dest_email():
    """Return the configured destination Kindle address, or raise MissingConfigError."""
    email = os.environ.get(ENV_DEST_EMAIL)
    if not email:
        raise MissingConfigError(
            f"{ENV_DEST_EMAIL} is not set -- set it to your Kindle's "
            f"@kindle.com address before using Send-to-Kindle delivery"
        )
    return email


def should_send_to_kindle(send_flag, output_format):
    """Resolve the --send-to-kindle/--no-send-to-kindle default.

    send_flag is the raw parsed CLI value: True/False if the user passed
    --send-to-kindle/--no-send-to-kindle explicitly, None if neither was
    passed. Send-to-Kindle only ever applies to EPUB output -- PDF is never
    auto-sent, even if --send-to-kindle was passed explicitly.
    """
    if output_format != "epub":
        return False
    return True if send_flag is None else send_flag


def get_app_password(username=None):
    """Return the Gmail App Password, or raise a KindleDeliveryError.

    Resolution order: OS keyring, then the MD_TO_KINDLE_APP_PASSWORD
    environment variable. Never includes the credential value in any
    raised exception message.
    """
    username = username or get_sender_email()
    backend_error = None
    password = None
    try:
        password = keyring.get_password(KEYRING_SERVICE, username)
    except keyring.errors.KeyringError as e:
        backend_error = e

    if password:
        return password

    env_password = os.environ.get(ENV_APP_PASSWORD)
    if env_password:
        return env_password

    if backend_error is not None:
        raise KeyringBackendError(
            f"keyring backend unavailable ({backend_error}); "
            f"set the {ENV_APP_PASSWORD} environment variable instead"
        )
    raise MissingCredentialError(
        "no Gmail App Password stored -- run "
        "'python3 md_to_kindle.py --set-kindle-password' first"
    )


def set_app_password(password, username=None):
    """Store the Gmail App Password in the OS keyring."""
    username = username or get_sender_email()
    try:
        keyring.set_password(KEYRING_SERVICE, username, password)
    except keyring.errors.KeyringError as e:
        raise KeyringBackendError(f"could not store credential in keyring: {e}") from e


def clear_app_password(username=None):
    """Remove the stored Gmail App Password from the OS keyring.

    A no-op (not an error) if nothing was stored.
    """
    username = username or get_sender_email()
    try:
        keyring.delete_password(KEYRING_SERVICE, username)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as e:
        raise KeyringBackendError(f"could not clear credential in keyring: {e}") from e


def send_to_kindle(file_path, sender_email=None, dest_email=None):
    """Email file_path to dest_email via Gmail SMTP.

    Success only means Gmail accepted the email -- Amazon's conversion/
    delivery into the Kindle library is a separate, asynchronous step this
    function cannot observe. Raises a KindleDeliveryError subclass on any
    failure; never leaves the source file touched.
    """
    if not os.path.exists(file_path):
        raise MissingFileError(f"file not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    mime = _ATTACHMENT_MIME.get(ext)
    if mime is None:
        raise UnsupportedFormatError(f"unsupported file extension for Kindle delivery: {ext}")
    maintype, subtype = mime

    sender_email = sender_email or get_sender_email()
    dest_email = dest_email or get_dest_email()
    password = get_app_password(sender_email)

    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        data = f.read()

    msg = EmailMessage()
    msg["Subject"] = filename
    msg["From"] = sender_email
    msg["To"] = dest_email
    msg.set_content(f"Sent automatically by markdown_to_kindle_format: {filename}")
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.login(sender_email, password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise SmtpAuthError(f"Gmail rejected the App Password: {e}") from e
    except (smtplib.SMTPException, OSError, TimeoutError) as e:
        raise SmtpConnectionError(f"could not send via {SMTP_HOST}:{SMTP_PORT}: {e}") from e
