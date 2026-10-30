"""Immutable credential references; secrets remain in the OS credential vault."""
import uuid

import keyring


SERVICE = 'douyin-live-assistant'


class CredentialError(RuntimeError):
    pass


def backend():
    value = keyring.get_keyring()
    if type(value).__module__ not in (
        'keyring.backends.Windows', 'keyring.backends.macOS',
        'keyring.backends.SecretService', 'keyring.backends.libsecret',
    ):
        raise CredentialError('secure_credential_store_unavailable')
    return value


def save_key(secret):
    reference = uuid.uuid4().hex
    try:
        backend().set_password(SERVICE, reference, secret)
    except Exception:
        raise CredentialError('credential_save_failed') from None
    return reference


def read_key(reference):
    if not reference:
        return ''
    try:
        secret = backend().get_password(SERVICE, reference)
    except Exception:
        raise CredentialError('credential_read_failed') from None
    if not secret:
        raise CredentialError('credential_not_found')
    return secret
