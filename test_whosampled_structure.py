#!/usr/bin/env python3

import sys
from html.parser import HTMLParser


class StructureParser(HTMLParser):

    def __init__(self):
        super().__init__()

        self.depth = 0
        self.in_dest = False
        self.in_source = False

        self.dest_depth = None
        self.source_depth = None

        self.dest_found = []
        self.source_found = []

    def handle_starttag(self, tag, attrs):

        attrs = dict(attrs)

        self.depth += 1

        element_id = attrs.get("id")
        itemprop = attrs.get("itemprop")

        # ----------------------------------------------------
        # Detect the two actual WhoSampled track containers
        # ----------------------------------------------------

        if element_id == "sampleWrap_dest":

            self.in_dest = True
            self.dest_depth = self.depth

            print("\n=== DESTINATION TRACK ===")
            print("Found: #sampleWrap_dest")

        elif element_id == "sampleWrap_source":

            self.in_source = True
            self.source_depth = self.depth

            print("\n=== SOURCE TRACK ===")
            print("Found: #sampleWrap_source")

        # ----------------------------------------------------
        # Show structured metadata inside each track
        # ----------------------------------------------------

        if self.in_dest or self.in_source:

            if itemprop:

                location = (
                    "DEST"
                    if self.in_dest
                    else "SOURCE"
                )

                extra = ""

                if "href" in attrs:
                    extra = f' href="{attrs["href"]}"'

                if "content" in attrs:
                    extra += f' content="{attrs["content"]}"'

                print(
                    f"[{location}] "
                    f"<{tag}> "
                    f'itemprop="{itemprop}"'
                    f"{extra}"
                )

        # ----------------------------------------------------
        # Timing IDs
        # ----------------------------------------------------

        if element_id in (
            "sample-dest-timing",
            "sample-source-timing",
        ):

            print(
                f"TIMING: #{element_id} "
                f'data-timings="{attrs.get("data-timings")}"'
            )

    def handle_endtag(self, tag):

        if (
            self.in_dest
            and self.depth == self.dest_depth
        ):

            self.in_dest = False
            self.dest_depth = None

        if (
            self.in_source
            and self.depth == self.source_depth
        ):

            self.in_source = False
            self.source_depth = None

        self.depth -= 1


def main():

    if len(sys.argv) != 2:

        print(
            "Usage:\n"
            "python test_whosampled_structure.py FILE"
        )

        return 1

    filename = sys.argv[1]

    print(f"Reading: {filename}")

    try:

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as f:

            html = f.read()

    except Exception as e:

        print(f"ERROR: {e}")

        return 1

    print(f"Bytes: {len(html)}")

    parser = StructureParser()
    parser.feed(html)

    print("\n=== DONE ===")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

