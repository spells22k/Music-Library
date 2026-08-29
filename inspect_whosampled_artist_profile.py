import re
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


URL = (
    "https://www.whosampled.com/"
    "Luiz-Melod%C3%ADa/"
)

OUTPUT = Path(
    "luiz_melodia_artist_profile_inspection.html"
)

TARGET_LABELS = [
    "Real Name",
    "Aliases",
    "In Groups",
    "Past Groups",
    "Members",
    "Group Members",
]


def clean(value):
    if value is None:
        return ""

    return " ".join(
        str(value).split()
    )


def print_element(
    element,
    heading,
):
    print()
    print("-" * 80)
    print(heading)
    print("-" * 80)

    if element is None:
        print("(none)")
        return

    print(
        element.prettify()[:5000]
    )


def tag_summary(tag):
    if tag is None:
        return "(none)"

    if not getattr(
        tag,
        "name",
        None,
    ):
        return repr(
            clean(tag)
        )

    attrs = dict(
        tag.attrs
    )

    return {
        "tag":
            tag.name,

        "attrs":
            attrs,

        "text":
            clean(
                tag.get_text(
                    " ",
                    strip=True,
                )
            ),
    }


print("=" * 80)
print("WHOSAMPLED ARTIST PROFILE STRUCTURE INSPECTION")
print("=" * 80)
print()
print("URL:", URL)

with sync_playwright() as p:

    browser = p.chromium.launch(
        headless=False
    )

    page = browser.new_page(
        viewport={
            "width": 1440,
            "height": 1000,
        }
    )

    response = page.goto(
        URL,
        wait_until="domcontentloaded",
        timeout=60000,
    )

    status = (
        response.status
        if response
        else None
    )

    print("STATUS:", status)
    print("FINAL URL:", page.url)

    if status != 200:
        browser.close()

        raise SystemExit(
            f"Expected 200; got {status}"
        )

    html = page.content()

    OUTPUT.write_text(
        html,
        encoding="utf-8",
    )

    print(
        "HTML SAVED:",
        OUTPUT,
    )

    print(
        "HTML BYTES:",
        len(
            html.encode(
                "utf-8"
            )
        ),
    )

    browser.close()


soup = BeautifulSoup(
    html,
    "html.parser",
)


print()
print("=" * 80)
print("PAGE IDENTITY")
print("=" * 80)

print(
    "TITLE:",
    clean(
        soup.title.get_text()
        if soup.title
        else ""
    ),
)

h1 = soup.find("h1")

print(
    "H1:",
    clean(
        h1.get_text(
            " ",
            strip=True,
        )
        if h1
        else ""
    ),
)


print()
print("=" * 80)
print("TARGET LABEL SEARCH")
print("=" * 80)


for target in TARGET_LABELS:

    print()
    print("#" * 80)
    print("TARGET:", target)
    print("#" * 80)

    matches = []

    normalized_target = (
        target
        .rstrip(":")
        .casefold()
    )

    for node in soup.find_all(
        string=True
    ):

        text = clean(
            node
        )

        normalized = (
            text
            .rstrip(":")
            .casefold()
        )

        if normalized == normalized_target:
            matches.append(
                node
            )

    print(
        "MATCHES:",
        len(matches),
    )

    for number, node in enumerate(
        matches,
        start=1,
    ):

        element = (
            node.parent
            if node.parent
            else None
        )

        print()
        print(
            f"MATCH {number}"
        )

        print(
            "LABEL ELEMENT:",
            tag_summary(
                element
            ),
        )

        parent = (
            element.parent
            if element
            else None
        )

        print(
            "PARENT:",
            tag_summary(
                parent
            ),
        )

        grandparent = (
            parent.parent
            if parent
            else None
        )

        print(
            "GRANDPARENT:",
            tag_summary(
                grandparent
            ),
        )

        previous = (
            element.find_previous_sibling()
            if element
            else None
        )

        next_sibling = (
            element.find_next_sibling()
            if element
            else None
        )

        print(
            "PREVIOUS SIBLING:",
            tag_summary(
                previous
            ),
        )

        print(
            "NEXT SIBLING:",
            tag_summary(
                next_sibling
            ),
        )

        if parent:

            children = [
                child
                for child in parent.children
                if getattr(
                    child,
                    "name",
                    None,
                )
            ]

            print(
                "PARENT CHILDREN:"
            )

            for i, child in enumerate(
                children,
                start=1,
            ):

                print(
                    f"  {i}.",
                    tag_summary(
                        child
                    ),
                )

            links = parent.find_all(
                "a",
                href=True,
            )

            if links:

                print(
                    "PARENT LINKS:"
                )

                for link in links:

                    print(
                        " ",
                        clean(
                            link.get_text(
                                " ",
                                strip=True,
                            )
                        ),
                        "→",
                        link.get(
                            "href"
                        ),
                    )

        print_element(
            parent,
            "FULL PARENT HTML",
        )

        print_element(
            grandparent,
            "FULL GRANDPARENT HTML",
        )


print()
print("=" * 80)
print("LIKELY PROFILE INFORMATION CONTAINERS")
print("=" * 80)

keywords = re.compile(
    r"(real.?name|aliases?|groups?|members?)",
    flags=re.IGNORECASE,
)

seen = set()

for tag in soup.find_all(
    ["div", "section", "ul", "dl", "table"]
):

    text = clean(
        tag.get_text(
            " ",
            strip=True,
        )
    )

    if not keywords.search(
        text
    ):
        continue

    if len(text) > 1200:
        continue

    signature = (
        tag.name,
        tuple(
            sorted(
                (
                    key,
                    str(value),
                )
                for key, value
                in tag.attrs.items()
            )
        ),
        text,
    )

    if signature in seen:
        continue

    seen.add(
        signature
    )

    print()
    print(
        "TAG:",
        tag.name,
    )

    print(
        "ATTRS:",
        tag.attrs,
    )

    print(
        "TEXT:",
        text,
    )

    print(
        "HTML:"
    )

    print(
        tag.prettify()[:4000]
    )


print()
print("=" * 80)
print("IMAGE CANDIDATES")
print("=" * 80)

for image in soup.find_all(
    "img",
):

    src = clean(
        image.get(
            "src"
        )
    )

    alt = clean(
        image.get(
            "alt"
        )
    )

    classes = " ".join(
        image.get(
            "class",
            [],
        )
    )

    if not src:
        continue

    print()
    print(
        "SRC:",
        src,
    )

    print(
        "ALT:",
        alt,
    )

    print(
        "CLASS:",
        classes,
    )

    print(
        "WIDTH:",
        image.get(
            "width"
        ),
    )

    print(
        "HEIGHT:",
        image.get(
            "height"
        ),
    )


print()
print("=" * 80)
print("META IMAGE CANDIDATES")
print("=" * 80)

for tag in soup.find_all(
    "meta"
):

    key = (
        tag.get("property")
        or tag.get("name")
        or ""
    )

    if "image" not in str(
        key
    ).casefold():
        continue

    print(
        key,
        "→",
        tag.get(
            "content"
        ),
    )


print()
print("=" * 80)
print("INSPECTION COMPLETE")
print("=" * 80)
