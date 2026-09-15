import re


def text_comparison_content(content: str) -> str:
    """Keep readable evidence, excluding image filenames and storage URLs.

    Image-only fragments remain in the source; text comparison cannot prove
    two images depict the same thing. Meaningful OCR content is retained.
    """
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "", content or "")
    lines = []
    for line in text.splitlines():
        link = re.fullmatch(r"\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if link and (
            "/kb-images/" in link[2]
            or re.search(r"\.(?:png|jpe?g|gif|webp|svg)(?:$|[?#])", link[1], re.I)
        ):
            continue
        lines.append(line)
    return re.sub(r"图片文字\s*[:：]", "", "\n".join(lines)).strip()
