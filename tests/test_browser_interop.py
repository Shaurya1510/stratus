"""The browser and the terminal must read each other's encryption.

The website does its cryptography with WebCrypto in JavaScript while the
terminal uses Python, so the two implementations are checked against each other
here rather than assumed to agree. Node provides the same WebCrypto API the
browser does; when Node is absent the check is skipped, since it is a
development safeguard rather than a runtime requirement.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.security.browser_access import seal
from core.security.e2ee import decrypt, encrypt, generate_ecdh_keypair

CRYPTO_JS = ROOT / "web" / "chat" / "static" / "stratus-crypto.js"

BRIDGE = r"""
const fs = require('fs');
globalThis.atob = (s) => Buffer.from(s, 'base64').toString('binary');
globalThis.btoa = (s) => Buffer.from(s, 'binary').toString('base64');
const CF = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
(async () => {
  const result = {};
  const pkcs8 = await CF.unsealDeviceKey(input.sealed, input.passphrase, input.device_id);
  result.unsealed = pkcs8 === input.private_key;
  try { await CF.unsealDeviceKey(input.sealed, 'not the passphrase', input.device_id); result.wrong_rejected = false; }
  catch { result.wrong_rejected = true; }
  const key = await CF.importPrivateKey(pkcs8);
  result.decrypted = await CF.decryptText(input.payload, key, input.device_id, input.aad);
  try { await CF.decryptText(input.payload, key, input.device_id, 'a-different-conversation'); result.aad_enforced = false; }
  catch { result.aad_enforced = true; }
  result.produced = await CF.encryptText(input.reply, input.recipients, input.aad);
  fs.writeFileSync(process.argv[4], JSON.stringify(result));
})().catch((error) => { console.error(error); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "Node is not installed; browser crypto check skipped")
class BrowserInteropTests(unittest.TestCase):
    def test_browser_and_terminal_read_each_others_encryption(self):
        alice_private, alice_public = generate_ecdh_keypair()
        bob_private, bob_public = generate_ecdh_keypair()
        alice = {"id": "device-alice", "display_name": "alice@linux", "ecdh_public_key": alice_public}
        bob = {"id": "device-bob", "display_name": "bob@linux", "ecdh_public_key": bob_public}
        payload = encrypt(b"written in the terminal", [alice, bob], b"general")

        request = {
            "sealed": seal(alice_private, "a good passphrase", "device-alice"),
            "passphrase": "a good passphrase", "private_key": alice_private,
            "device_id": "device-alice", "payload": payload, "aad": "general",
            "reply": "written in the browser", "recipients": [alice, bob],
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            work = Path(folder)
            (work / "bridge.js").write_text(BRIDGE, encoding="utf-8")
            (work / "input.json").write_text(json.dumps(request), encoding="utf-8")
            completed = subprocess.run(
                ["node", str(work / "bridge.js"), str(CRYPTO_JS), str(work / "input.json"), str(work / "output.json")],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads((work / "output.json").read_text(encoding="utf-8"))

        self.assertTrue(result["unsealed"], "browser could not unseal the passphrase-protected key")
        self.assertTrue(result["wrong_rejected"], "a wrong passphrase unsealed the key")
        self.assertEqual(result["decrypted"], "written in the terminal")
        self.assertTrue(result["aad_enforced"], "a message opened under the wrong conversation")

        produced = result["produced"]
        self.assertEqual(produced["version"], 2)
        self.assertEqual(set(produced["envelopes"]), {"device-alice", "device-bob"})
        for device_id, private_key in (("device-alice", alice_private), ("device-bob", bob_private)):
            identity = {"id": device_id, "ecdh_private_key": private_key}
            self.assertEqual(decrypt(produced, identity, b"general"), b"written in the browser")


if __name__ == "__main__": unittest.main()
