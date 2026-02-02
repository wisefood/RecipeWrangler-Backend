import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


# Purpose: Export FoodOn OWL classes and subclass edges to JSON.

DEFAULT_INPUT = "./data/raw/foodon/foodon.owl"
DEFAULT_OUTPUT = "./data/processed/foodon/foodon-ontofox.json"

NS = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}

OBO_PREFIX = "http://purl.obolibrary.org/obo/"


def uri_to_id(uri: str) -> str:
    if uri.startswith(OBO_PREFIX):
        return uri.split("/")[-1]
    return uri


def main():
    parser = argparse.ArgumentParser(
        description="Export all OWL classes + subclass edges to JSON."
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT,
        help=f"Path to foodon.owl (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to write JSON (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    tree = ET.parse(input_path)
    root = tree.getroot()

    nodes = {}
    edges = []

    def add_node(uri: str, label: str | None) -> str:
        if uri not in nodes:
            nodes[uri] = {"id": uri_to_id(uri), "uri": uri, "label": label}
        elif label and not nodes[uri].get("label"):
            nodes[uri]["label"] = label
        return nodes[uri]["id"]

    for cls in root.findall("owl:Class", NS):
        uri = cls.attrib.get(f"{{{NS['rdf']}}}about")
        if not uri:
            continue

        label = None
        label_el = cls.find("rdfs:label", NS)
        if label_el is not None and label_el.text:
            label = label_el.text.strip()

        child_id = add_node(uri, label)

        for parent in cls.findall("rdfs:subClassOf", NS):
            parent_uri = parent.attrib.get(f"{{{NS['rdf']}}}resource")
            if parent_uri:
                parent_id = add_node(parent_uri, None)
                edges.append({"child": child_id, "parent": parent_id})

    data = {"nodes": list(nodes.values()), "edges": edges}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Wrote {len(nodes)} nodes and {len(edges)} edges to {output_path}")


if __name__ == "__main__":
    main()
