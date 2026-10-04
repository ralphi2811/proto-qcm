"""Format du contenu du QR code.

Le QR contient une chaîne Base45 (jeu de caractères = mode alphanumérique QR, donc
dense) d'un blob binaire :

    en-tête clair (8 o) | IV (12 o) | AES-GCM(chiffré + tag 8 o)

En-tête clair (authentifié comme AAD, non secret) :
    [0] version   [1] nb questions   [2] nb options max   [3] taille des cases (0 std, 1 grande)
    [4:8] identifiant d'examen aléatoire

Le chiffrement/déchiffrement est fait uniquement côté navigateur (WebCrypto) avec la
clé de l'enseignant : le serveur ne voit jamais le corrigé, il n'a besoin que de
l'en-tête pour connaître la géométrie de la grille.
"""

from __future__ import annotations

from dataclasses import dataclass

B45 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
B45_IDX = {c: i for i, c in enumerate(B45)}

FORMAT_VERSION = 1
HEADER_LEN = 8


class PayloadError(ValueError):
    pass


def b45decode(s: str) -> bytes:
    out = bytearray()
    try:
        vals = [B45_IDX[c] for c in s]
    except KeyError as e:
        raise PayloadError(f"Caractère Base45 invalide : {e}") from None
    for i in range(0, len(vals), 3):
        chunk = vals[i : i + 3]
        if len(chunk) == 3:
            n = chunk[0] + chunk[1] * 45 + chunk[2] * 45 * 45
            if n > 0xFFFF:
                raise PayloadError("Base45 invalide")
            out += n.to_bytes(2, "big")
        elif len(chunk) == 2:
            n = chunk[0] + chunk[1] * 45
            if n > 0xFF:
                raise PayloadError("Base45 invalide")
            out.append(n)
        else:
            raise PayloadError("Longueur Base45 invalide")
    return bytes(out)


def b45encode(b: bytes) -> str:
    out = []
    for i in range(0, len(b), 2):
        chunk = b[i : i + 2]
        if len(chunk) == 2:
            n = (chunk[0] << 8) | chunk[1]
            c, n = divmod(n, 45 * 45)
            e, d = divmod(n, 45)
            out += [B45[d], B45[e], B45[c]]
        else:
            e, d = divmod(chunk[0], 45)
            out += [B45[d], B45[e]]
    return "".join(out)


@dataclass
class Header:
    version: int
    n_questions: int
    n_options: int
    style: int
    exam_id: str  # hex

    def to_json(self) -> dict:
        return self.__dict__.copy()


def parse_blob(raw: bytes) -> Header:
    """En-tête du blob examen produit par le navigateur."""
    if len(raw) < HEADER_LEN + 12 + 8:
        raise PayloadError("QR trop court")
    if raw[0] != FORMAT_VERSION:
        raise PayloadError(f"Version de format inconnue : {raw[0]}")
    return Header(raw[0], raw[1], raw[2], raw[3], raw[4:8].hex())


def parse_header(qr_text: str) -> Header:
    return parse_blob(b45decode(qr_text.strip()))


def parse_qr(qr_text: str):
    """QR lu sur une copie : grille (blob seul) ou page de sujet (partie page + blob).
    Renvoie (Header, PageInfo | None)."""
    from . import pageqr  # import local : évite un cycle

    raw = b45decode(qr_text.strip())
    if raw and raw[0] == pageqr.PAGE_FORMAT:
        info, blob = pageqr.decode(raw)
        return parse_blob(blob), info
    return parse_blob(raw), None
