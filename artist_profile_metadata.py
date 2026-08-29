import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def unique(values):
    result = []
    seen = set()

    for value in values:
        value = clean(value)

        if not value:
            continue

        key = value.casefold()

        if key in seen:
            continue

        seen.add(key)
        result.append(value)

    return result


def split_values(value):
    value = clean(value)

    if not value:
        return []

    return [
        part.strip()
        for part in re.split(
            r"\s*(?:,|;|\|| / )\s*",
            value,
        )
        if part.strip()
    ]


def local_image_data_uri(path):
    path = Path(
        clean(path)
    )

    if not path.exists():
        return ""

    try:
        data = path.read_bytes()
    except Exception:
        return ""

    mime_type, _ = mimetypes.guess_type(
        path.name
    )

    if not mime_type:
        mime_type = "image/jpeg"

    encoded = base64.b64encode(
        data
    ).decode(
        "ascii"
    )

    return (
        f"data:{mime_type};base64,"
        f"{encoded}"
    )


def canonical_url(soup):
    tag = soup.find(
        "link",
        rel=lambda value: (
            value
            and (
                "canonical"
                in (
                    value
                    if isinstance(
                        value,
                        list,
                    )
                    else [value]
                )
            )
        ),
    )

    if tag:
        return clean(
            tag.get("href")
        )

    tag = soup.find(
        "meta",
        attrs={
            "property": "og:url",
        },
    )

    if tag:
        return clean(
            tag.get("content")
        )

    return ""


def profile_name(soup):
    h1 = soup.find("h1")

    if h1:
        return clean(
            h1.get_text(
                " ",
                strip=True,
            )
        )

    tag = soup.find(
        "meta",
        attrs={
            "property": "og:title",
        },
    )

    if tag:
        return clean(
            tag.get("content")
        )

    return ""


def image_candidates(
    soup,
    base_url,
):
    """
    Return artist-profile image candidates in descending preference.

    WhoSampled artist pages currently expose:

        .artistImage img[itemprop="image"]

    with a 100px src and often a 200px srcset variant.

    og:image is also commonly the 200px artist image.
    """

    result = []

    # --------------------------------------------------------
    # 1. Explicit WhoSampled artist image.
    # --------------------------------------------------------

    artist_image = soup.select_one(
        ".artistHeader .artistImage "
        "img[itemprop='image'], "
        ".artistImage img"
    )

    if artist_image:

        srcset = clean(
            artist_image.get(
                "srcset"
            )
        )

        if srcset:

            srcset_candidates = []

            for entry in srcset.split(","):

                parts = (
                    entry.strip()
                    .split()
                )

                if not parts:
                    continue

                url = parts[0]

                scale = 1.0

                if (
                    len(parts) > 1
                    and parts[1].endswith(
                        "x"
                    )
                ):
                    try:
                        scale = float(
                            parts[1][:-1]
                        )
                    except Exception:
                        scale = 1.0

                srcset_candidates.append(
                    (
                        scale,
                        urljoin(
                            base_url,
                            url,
                        ),
                    )
                )

            srcset_candidates.sort(
                reverse=True,
            )

            result.extend(
                url
                for _, url
                in srcset_candidates
            )

        src = clean(
            artist_image.get(
                "src"
            )
        )

        if src:
            result.append(
                urljoin(
                    base_url,
                    src,
                )
            )

    # --------------------------------------------------------
    # 2. Metadata images.
    # --------------------------------------------------------

    for attrs in (
        {
            "property": "og:image",
        },
        {
            "name": "twitter:image",
        },
        {
            "property": "twitter:image",
        },
    ):

        tag = soup.find(
            "meta",
            attrs=attrs,
        )

        if not tag:
            continue

        src = clean(
            tag.get(
                "content"
            )
        )

        if src:
            result.append(
                urljoin(
                    base_url,
                    src,
                )
            )

    return unique(
        result
    )

def labelled_pairs(soup):
    """
    Extract artist-profile metadata label/value pairs.

    WhoSampled currently uses structures such as:

        <div class="meta-item">
            <span>Real Name:</span>
            <h2 itemprop="legalName">
                Luiz Carlos dos Santos
            </h2>
        </div>

    We handle that explicitly before generic fallbacks.
    """

    pairs = []
    seen = set()

    def add_pair(label, value):

        label = clean(label).rstrip(":")
        value = clean(value)

        if not label or not value:
            return

        key = (
            label.casefold(),
            value.casefold(),
        )

        if key in seen:
            return

        seen.add(key)

        pairs.append(
            (
                label,
                value,
            )
        )

    # --------------------------------------------------------
    # 1. Native WhoSampled artist profile metadata structure.
    # --------------------------------------------------------

    for item in soup.select(
        ".media-metainfo .meta-item, "
        ".metainfo-wrapper .meta-item"
    ):

        label_node = item.find(
            "span"
        )

        if not label_node:
            continue

        label = clean(
            label_node.get_text(
                " ",
                strip=True,
            )
        )

        # Prefer semantic value elements.
        value_node = item.find(
            attrs={
                "itemprop": True,
            }
        )

        if (
            value_node is None
            or value_node is label_node
        ):

            value_node = (
                label_node
                .find_next_sibling()
            )

        if value_node:

            add_pair(
                label,
                value_node.get_text(
                    " ",
                    strip=True,
                ),
            )

    # --------------------------------------------------------
    # 2. Definition-list fallback.
    # --------------------------------------------------------

    for dt in soup.find_all("dt"):

        dd = dt.find_next_sibling(
            "dd"
        )

        if not dd:
            continue

        add_pair(
            dt.get_text(
                " ",
                strip=True,
            ),
            dd.get_text(
                " ",
                strip=True,
            ),
        )

    # --------------------------------------------------------
    # 3. Table fallback.
    # --------------------------------------------------------

    for tr in soup.find_all("tr"):

        cells = tr.find_all(
            ["th", "td"],
        )

        if len(cells) < 2:
            continue

        add_pair(
            cells[0].get_text(
                " ",
                strip=True,
            ),
            cells[1].get_text(
                " ",
                strip=True,
            ),
        )

    # --------------------------------------------------------
    # 4. Generic "Label: Value" fallback.
    # --------------------------------------------------------

    for element in soup.select(
        "li, p, .profileInfo div, "
        ".profile-info div, .info div"
    ):

        text = clean(
            element.get_text(
                " ",
                strip=True,
            )
        )

        if ":" not in text:
            continue

        label, value = text.split(
            ":",
            1,
        )

        add_pair(
            label,
            value,
        )

    return pairs

def _unique_entities(entities):
    result = []
    seen = set()

    for entity in entities:

        if not isinstance(
            entity,
            dict,
        ):
            continue

        name = clean(
            entity.get(
                "name"
            )
        )

        url = clean(
            entity.get(
                "whosampled_url"
            )
        )

        if not name:
            continue

        key = (
            name.casefold(),
            url.rstrip("/").casefold(),
        )

        if key in seen:
            continue

        seen.add(key)

        result.append({
            "name":
                name,

            "whosampled_url":
                url,
        })

    return result


def _extract_entities_from_value(
    value_node,
    base_url,
    itemprop=None,
):
    """
    Extract named WhoSampled entities while preserving an
    optional profile URL.

    Example confirmed on Secos & Molhados:

        <span itemprop="member" ...>
            <span itemprop="name">
                <a href="/Ney-Matogrosso/"
                   itemprop="url">
                    Ney Matogrosso
                </a>
            </span>
        </span>
    """

    if value_node is None:
        return []

    result = []

    if itemprop:

        containers = value_node.select(
            f'[itemprop="{itemprop}"]'
        )

    else:

        containers = []

    for container in containers:

        name_node = (
            container.select_one(
                '[itemprop="name"]'
            )
            or container
        )

        name = clean(
            name_node.get_text(
                " ",
                strip=True,
            )
        )

        if not name:
            continue

        link = name_node.find(
            "a",
            href=True,
        )

        if link is None:

            link = container.find(
                "a",
                href=True,
            )

        url = ""

        if link is not None:

            url = urljoin(
                base_url,
                clean(
                    link.get(
                        "href"
                    )
                ),
            )

        result.append({
            "name":
                name,

            "whosampled_url":
                url,
        })

    if result:

        return _unique_entities(
            result
        )

    # Link-based fallback for other WhoSampled metadata layouts.
    for link in value_node.find_all(
        "a",
        href=True,
    ):

        name = clean(
            link.get_text(
                " ",
                strip=True,
            )
        )

        if not name:
            continue

        result.append({
            "name":
                name,

            "whosampled_url":
                urljoin(
                    base_url,
                    clean(
                        link.get(
                            "href"
                        )
                    ),
                ),
        })

    return _unique_entities(
        result
    )


def parse_profile_html(
    html_text,
    requested_url,
):
    soup = BeautifulSoup(
        html_text,
        "html.parser",
    )

    result = {
        "requested_url":
            requested_url,

        "canonical_url":
            canonical_url(
                soup
            ),

        "profile_name":
            profile_name(
                soup
            ),

        "real_names":
            [],

        "aliases":
            [],

        # Compatibility field. Contains names only.
        "groups":
            [],

        # Structured artist-to-group identities.
        "current_groups":
            [],

        "past_groups":
            [],

        # Structured group-to-artist identities.
        "group_members":
            [],

        "country":
            [],

        "image_url":
            "",

        "image_path":
            "",

        "status":
            "parsed",
    }

    # --------------------------------------------------------
    # Native WhoSampled artist metadata.
    #
    # Confirmed:
    #
    # <div class="meta-item">
    #   <span>Real Name:</span>
    #   <h2 itemprop="legalName">...</h2>
    # </div>
    #
    # and:
    #
    # <div class="meta-item">
    #   <span>Group Members:</span>
    #   <h2>
    #       <span itemprop="member">...</span>
    #   </h2>
    # </div>
    # --------------------------------------------------------

    for item in soup.select(
        ".media-metainfo .meta-item, "
        ".metainfo-wrapper .meta-item"
    ):

        # The outer/direct span is the field label.
        label_node = item.find(
            "span",
            recursive=False,
        )

        if label_node is None:

            label_node = item.find(
                "span"
            )

        if label_node is None:
            continue

        label = (
            clean(
                label_node.get_text(
                    " ",
                    strip=True,
                )
            )
            .rstrip(":")
            .casefold()
        )

        value_node = (
            label_node
            .find_next_sibling()
        )

        if value_node is None:
            continue

        # ----------------------------------------------------
        # Real / legal name.
        # ----------------------------------------------------

        if label in {
            "real name",
            "realname",
            "birth name",
            "legal name",
        }:

            value = clean(
                value_node.get_text(
                    " ",
                    strip=True,
                )
            )

            if value:

                result[
                    "real_names"
                ].append(
                    value
                )

            continue

        # ----------------------------------------------------
        # Aliases.
        # ----------------------------------------------------

        if label in {
            "alias",
            "aliases",
            "aka",
            "also known as",
        }:

            names = [
                clean(
                    node.get_text(
                        " ",
                        strip=True,
                    )
                )
                for node
                in value_node.select(
                    '[itemprop="alternateName"]'
                )
            ]

            if not names:

                names = [
                    clean(
                        node.get_text(
                            " ",
                            strip=True,
                        )
                    )
                    for node
                    in value_node.select(
                        '[itemprop="name"]'
                    )
                ]

            if not names:

                names = split_values(
                    value_node.get_text(
                        " ",
                        strip=True,
                    )
                )

            result[
                "aliases"
            ].extend(
                x
                for x in names
                if x
            )

            continue

        # ----------------------------------------------------
        # Group -> members.
        #
        # Preserve every member as:
        #
        # {
        #   "name": "...",
        #   "whosampled_url": "..."
        # }
        #
        # The URL is empty when WhoSampled gives a name but no
        # dedicated profile link.
        # ----------------------------------------------------

        if label in {
            "group members",
            "members",
        }:

            members = (
                _extract_entities_from_value(
                    value_node,
                    requested_url,
                    itemprop="member",
                )
            )

            if not members:

                members = [
                    {
                        "name":
                            name,

                        "whosampled_url":
                            "",
                    }
                    for name
                    in split_values(
                        value_node.get_text(
                            " ",
                            strip=True,
                        )
                    )
                ]

            result[
                "group_members"
            ].extend(
                members
            )

            continue

        # ----------------------------------------------------
        # Individual -> current groups.
        #
        # We support several plausible label variants, but do
        # not fabricate any data when they are absent.
        # ----------------------------------------------------

        if label in {
            "in groups",
            "in group",
            "groups",
            "group",
            "member of",
            "current groups",
        }:

            groups = (
                _extract_entities_from_value(
                    value_node,
                    requested_url,
                    itemprop="memberOf",
                )
            )

            if not groups:

                groups = (
                    _extract_entities_from_value(
                        value_node,
                        requested_url,
                    )
                )

            if not groups:

                groups = [
                    {
                        "name":
                            name,

                        "whosampled_url":
                            "",
                    }
                    for name
                    in split_values(
                        value_node.get_text(
                            " ",
                            strip=True,
                        )
                    )
                ]

            result[
                "current_groups"
            ].extend(
                groups
            )

            continue

        # ----------------------------------------------------
        # Individual -> former groups.
        # ----------------------------------------------------

        if label in {
            "past groups",
            "past group",
            "former groups",
            "former group",
        }:

            groups = (
                _extract_entities_from_value(
                    value_node,
                    requested_url,
                )
            )

            if not groups:

                groups = [
                    {
                        "name":
                            name,

                        "whosampled_url":
                            "",
                    }
                    for name
                    in split_values(
                        value_node.get_text(
                            " ",
                            strip=True,
                        )
                    )
                ]

            result[
                "past_groups"
            ].extend(
                groups
            )

            continue

        # ----------------------------------------------------
        # Country / origin.
        # ----------------------------------------------------

        if label in {
            "country",
            "origin",
            "nationality",
        }:

            result[
                "country"
            ].extend(
                split_values(
                    value_node.get_text(
                        " ",
                        strip=True,
                    )
                )
            )

    # --------------------------------------------------------
    # Generic fallback for simple metadata layouts.
    #
    # This deliberately excludes membership fields because the
    # semantic pass above preserves substantially more identity
    # information than a flattened comma-separated string.
    # --------------------------------------------------------

    mapping = {
        "real name":
            "real_names",

        "realname":
            "real_names",

        "birth name":
            "real_names",

        "legal name":
            "real_names",

        "alias":
            "aliases",

        "aliases":
            "aliases",

        "aka":
            "aliases",

        "also known as":
            "aliases",

        "country":
            "country",

        "origin":
            "country",

        "nationality":
            "country",
    }

    for label, value in labelled_pairs(
        soup
    ):

        key = (
            " ".join(
                label
                .rstrip(":")
                .casefold()
                .split()
            )
        )

        target = mapping.get(
            key
        )

        if not target:
            continue

        result[target].extend(
            split_values(
                value
            )
        )

    # --------------------------------------------------------
    # Deduplication.
    # --------------------------------------------------------

    for key in (
        "real_names",
        "aliases",
        "country",
    ):

        result[key] = unique(
            result[key]
        )

    result[
        "group_members"
    ] = _unique_entities(
        result[
            "group_members"
        ]
    )

    result[
        "current_groups"
    ] = _unique_entities(
        result[
            "current_groups"
        ]
    )

    result[
        "past_groups"
    ] = _unique_entities(
        result[
            "past_groups"
        ]
    )

    # Backward-compatible names-only aggregate.
    result[
        "groups"
    ] = unique(
        [
            item["name"]
            for item in (
                result[
                    "current_groups"
                ]
                + result[
                    "past_groups"
                ]
            )
            if clean(
                item.get(
                    "name"
                )
            )
        ]
    )

    images = image_candidates(
        soup,
        requested_url,
    )

    if images:

        result[
            "image_url"
        ] = images[0]

    return result

def safe_filename(value):
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        clean(value),
    )

    return (
        value.strip("_")
        or "artist"
    )


def load_cache(cache_file):
    cache_file = Path(
        cache_file
    )

    if not cache_file.exists():
        return {}

    try:
        data = json.loads(
            cache_file.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(
            data,
            dict,
        ):
            return data

    except Exception:
        pass

    return {}


def save_cache(
    cache_file,
    cache,
):
    cache_file = Path(
        cache_file
    )

    cache_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file.write_text(
        json.dumps(
            cache,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def collect_artist_profile_metadata(
    rows,
    cache_dir,
    delay_seconds=12,
):
    """
    Collect metadata for all unique review candidate profiles.

    Previously cached URLs cause zero new WhoSampled requests.
    """

    cache_dir = Path(
        cache_dir
    )

    cache_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    media_dir = (
        cache_dir
        / "media"
    )

    media_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = (
        cache_dir
        / "profiles.json"
    )

    cache = load_cache(
        cache_file
    )

    wanted = []

    seen = set()

    for row in rows:
        for candidate in row.get(
            "candidates",
            [],
        ):
            url = clean(
                candidate.get(
                    "url"
                )
            )

            if (
                not url
                or url in cache
                or url in seen
            ):
                continue

            seen.add(url)

            wanted.append({
                "spotify_artist_id":
                    clean(
                        row.get(
                            "spotify_artist_id"
                        )
                    ),

                "spotify_artist_name":
                    clean(
                        row.get(
                            "spotify_artist_name"
                        )
                    ),

                "url":
                    url,
            })

    if not wanted:
        return cache

    print()
    print("=" * 80)
    print(
        "WHOSAMPLED ARTIST PROFILE "
        "METADATA COLLECTION"
    )
    print("=" * 80)

    with sync_playwright() as p:

        browser = (
            p.chromium.launch(
                headless=False
            )
        )

        context = (
            browser.new_context()
        )

        page = (
            context.new_page()
        )

        for number, item in enumerate(
            wanted,
            start=1,
        ):
            print()
            print(
                f"[{number}/{len(wanted)}]",
                item[
                    "spotify_artist_name"
                ],
            )

            print(
                "PROFILE:",
                item["url"],
            )

            try:
                response = page.goto(
                    item["url"],
                    wait_until=
                        "domcontentloaded",
                    timeout=60000,
                )

                status = (
                    response.status
                    if response
                    else None
                )

                print(
                    "STATUS:",
                    status,
                )

                if status == 429:
                    raise SystemExit(
                        "Stopped safely on HTTP 429."
                    )

                if status != 200:
                    cache[
                        item["url"]
                    ] = {
                        "requested_url":
                            item["url"],

                        "status":
                            f"http_{status}",
                    }

                    save_cache(
                        cache_file,
                        cache,
                    )

                    continue

                metadata = (
                    parse_profile_html(
                        page.content(),
                        item["url"],
                    )
                )

                image_url = clean(
                    metadata.get(
                        "image_url"
                    )
                )

                if image_url:
                    try:
                        image_response = (
                            context.request.get(
                                image_url,
                                timeout=60000,
                            )
                        )

                        if image_response.ok:

                            content_type = (
                                clean(
                                    image_response
                                    .headers
                                    .get(
                                        "content-type"
                                    )
                                )
                                .split(
                                    ";",
                                    1,
                                )[0]
                            )

                            extension = (
                                mimetypes
                                .guess_extension(
                                    content_type
                                )
                                or ".jpg"
                            )

                            path = (
                                media_dir
                                / (
                                    safe_filename(
                                        item[
                                            "spotify_artist_id"
                                        ]
                                        + "_"
                                        + str(number)
                                    )
                                    + extension
                                )
                            )

                            path.write_bytes(
                                image_response.body()
                            )

                            metadata[
                                "image_path"
                            ] = str(path)

                    except Exception as exc:
                        print(
                            "IMAGE SAVE WARNING:",
                            repr(exc),
                        )

                cache[
                    item["url"]
                ] = metadata

                save_cache(
                    cache_file,
                    cache,
                )

                print(
                    "NAME:",
                    metadata.get(
                        "profile_name"
                    ),
                )

                print(
                    "REAL NAME:",
                    metadata.get(
                        "real_names"
                    ),
                )

                print(
                    "ALIASES:",
                    metadata.get(
                        "aliases"
                    ),
                )

                print(
                    "GROUPS:",
                    metadata.get(
                        "groups"
                    ),
                )

                print(
                    "GROUP MEMBERS:",
                    metadata.get(
                        "group_members"
                    ),
                )

            except SystemExit:
                raise

            except Exception as exc:

                cache[
                    item["url"]
                ] = {
                    "requested_url":
                        item["url"],

                    "status":
                        "error",

                    "error":
                        repr(exc),
                }

                save_cache(
                    cache_file,
                    cache,
                )

                print(
                    "PROFILE ERROR:",
                    repr(exc),
                )

            if number < len(wanted):

                print(
                    f"Waiting {delay_seconds} "
                    "seconds before next "
                    "WhoSampled request..."
                )

                time.sleep(
                    delay_seconds
                )

        browser.close()

    return cache
