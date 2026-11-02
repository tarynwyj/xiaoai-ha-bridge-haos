"""Real local TLS handshake regression for the generated Xiaomi login helper.

No network access to Xiaomi: the test creates a temporary self-signed HTTPS
server, ensures default aiohttp verification rejects it, and proves the same
helper succeeds when the test CA is explicitly trusted.
"""
import asyncio
import importlib.util
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

# test_api_auth selects the generated runtime at import time.
if len(sys.argv) < 2:
    sys.argv.append("/tmp/app")
from test_api_auth import build_runtime, load_bridge


async def _run(bridge, certificate: Path, private_key: Path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(private_key))
    messages = []

    async def respond(reader, writer):
        try:
            await reader.read(8192)
            messages.append("request")
            body = b'{"code":0,"sid":"micoapi"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + b"Content-Length: " + str(len(body)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0, ssl=context)
    try:
        port = server.sockets[0].getsockname()[1]
        original_base = bridge.OTP_ACCOUNT_BASE
        bridge.OTP_ACCOUNT_BASE = f"https://127.0.0.1:{port}"
        try:
            denied = False
            async with aiohttp.ClientSession() as session:
                try:
                    await bridge._mi_service_login_verified(
                        session, "serviceLogin?sid=micoapi&_json=true",
                        device_id="TESTDEVICE"
                    )
                except bridge.OtpFlowError:
                    denied = True
            assert denied, "default aiohttp TLS must reject a self-signed certificate"
            assert messages == [], "untrusted server must not receive account request body"

            trusted = ssl.create_default_context(cafile=str(certificate))
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=trusted)
            ) as session:
                result = await bridge._mi_service_login_verified(
                    session, "serviceLogin?sid=micoapi&_json=true",
                    device_id="TESTDEVICE"
                )
            assert result["code"] == 0 and result["sid"] == "micoapi", result
            assert messages == ["request"], messages
        finally:
            bridge.OTP_ACCOUNT_BASE = original_base
    finally:
        server.close()
        await server.wait_closed()


def main():
    assert shutil.which("openssl"), "OpenSSL is required to test real TLS handshakes"
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="xiaoai-tls-") as tmp:
        root = build_runtime(Path(tmp))
        cert = root / "test-cert.pem"
        key = root / "test-key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "1", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=IP:127.0.0.1",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True, timeout=30
        )
        try:
            os.chdir(root)
            bridge = load_bridge(root)
            asyncio.run(_run(bridge, cert, key))
        finally:
            os.chdir(original_cwd)
    print("real TLS handshake test passed: self-signed rejected, explicit trust accepted")


if __name__ == "__main__":
    main()
