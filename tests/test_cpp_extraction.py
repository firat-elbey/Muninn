"""Check C and C++ definitions without promoting type references."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from muninn import extract


@unittest.skipUnless(extract.available(), "tree-sitter not installed")
class TestCppExtraction(unittest.TestCase):
    def graph(self, source, extension):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fixture" + extension)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source)
            return extract.extract_file(path)

    def test_cpp_class_and_struct_keep_method_ownership(self):
        graph = self.graph(
            "class Queue { public: int size() { return 1; } };\n"
            "struct Packet { int value; };\n", ".cpp")
        nodes = {node["label"]: node for node in graph["nodes"]}
        self.assertEqual(set(nodes), {"fixture.cpp", "Queue", "Packet", "size"})
        self.assertEqual(nodes["Queue"]["kind"], "class")
        self.assertEqual(nodes["Packet"]["kind"], "struct")
        contains = {(edge["source"], edge["target"]) for edge in graph["links"]
                    if edge["relation"] == "contains"}
        self.assertIn((nodes["Queue"]["id"], nodes["size"]["id"]), contains)
        self.assertNotIn((nodes["fixture.cpp"]["id"], nodes["size"]["id"]), contains)

    def test_c_struct_references_do_not_duplicate_definitions(self):
        graph = self.graph(
            "struct Record { int value; };\n"
            "struct Holder { struct Record *first; struct Record *second; };\n"
            "int consume(struct Record *record) { return record->value; }\n", ".c")
        labels = [node["label"] for node in graph["nodes"]]
        self.assertEqual(labels.count("Record"), 1)
        self.assertEqual(labels.count("Holder"), 1)
        self.assertEqual(set(labels), {"fixture.c", "Record", "Holder", "consume"})

    def test_cpp_field_and_parameter_types_do_not_create_definitions(self):
        graph = self.graph(
            "class Queue;\nstruct Packet;\n"
            "struct Wrapper { class Queue *queue; struct Packet *packet; };\n"
            "void consume(class Queue *queue, struct Packet *packet) {}\n", ".cpp")
        labels = {node["label"] for node in graph["nodes"]}
        self.assertEqual(labels, {"fixture.cpp", "Wrapper", "consume"})

    def test_c_bodyless_types_remain_references(self):
        graph = self.graph(
            "struct Record;\n"
            "struct Record *current;\n"
            "void consume(struct Record *record);\n", ".c")
        self.assertEqual([node["label"] for node in graph["nodes"]], ["fixture.c"])

    def test_cpp_template_parameters_are_not_class_definitions(self):
        graph = self.graph(
            "template<class Item> class Box { Item value; };\n", ".cpp")
        nodes = {node["label"]: node for node in graph["nodes"]}
        self.assertEqual(set(nodes), {"fixture.cpp", "Box"})
        self.assertEqual(nodes["Box"]["kind"], "class")


class TestDefinitionFilter(unittest.TestCase):
    def test_nondefinition_specifiers_and_declarators_stay_excluded(self):
        for kind in ("mutable_specifier", "access_specifier", "storage_class_specifier",
                     "virtual_function_specifier", "function_declarator",
                     "tuple_struct_pattern", "class_type_identifier",
                     "class_specifier", "struct_specifier"):
            with self.subTest(kind=kind):
                self.assertIsNone(extract._def_kind(kind))
