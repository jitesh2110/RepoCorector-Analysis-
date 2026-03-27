import os
import zipfile
import shutil
import tempfile
import hashlib
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from tree_sitter import Language, Parser
import tree_sitter_javascript as tsjavascript

app = Flask(__name__)
CORS(app)

# Setup Tree-sitter
JS_LANGUAGE = Language(tsjavascript.language())
parser = Parser(JS_LANGUAGE)


def get_node_text(node, source_code):
    return source_code[node.start_byte:node.end_byte].decode('utf-8')


class RepoAnalyzer:
    def __init__(self, repo_path):
        self.repo_path = repo_path
        self.issues = []
        self.function_map = {}

    def analyze(self):
        for root, _, files in os.walk(self.repo_path):
            for file in files:
                if file.endswith(('.jsx', '.js')):
                    path = os.path.join(root, file)
                    self._process_file(path, file)
        self._check_duplicates()
        return {"issues": self.issues}

    def _process_file(self, path, filename):
        try:
            with open(path, 'rb') as f:
                source_code = f.read()
                tree = parser.parse(source_code)
                root_node = tree.root_node

                # Deep Analysis
                self._advanced_react_scanner(root_node, source_code, filename)
                self._manual_function_scan(root_node, source_code, filename)
        except Exception as e:
            print(f"Error: {filename}: {e}")

    def _advanced_react_scanner(self, root_node, source_code, filename):
        """
        Powerful detection of API calls and State synchronization smells.
        """
        module_id = filename.split('.')[0].lower() + "Stats"

        # Trackers for the current file
        found_states = []
        api_calls = []
        effect_blocks = []

        def walk(node):
            # 1. Smarter API Detection
            # Looks for fetch, axios, or any variable ending in 'Service' or 'Api'
            if node.type == 'call_expression':
                func_node = node.child_by_field_name('function')
                if func_node:
                    name = get_node_text(func_node, source_code)
                    if any(key in name.lower() for key in ['fetch', 'axios', 'api', 'getdata', 'postrequest']):
                        api_calls.append(name)

                    # 2. State & Effect extraction
                    if name == 'useState':
                        parent = node.parent
                        if parent and parent.type == 'array_pattern':
                            ids = [c for c in parent.children if c.type == 'identifier']
                            if len(ids) >= 2:
                                found_states.append({
                                    "name": get_node_text(ids[0], source_code),
                                    "setter": get_node_text(ids[1], source_code)
                                })

                    if name == 'useEffect':
                        effect_blocks.append(node)

            for child in node.children:
                walk(child)

        walk(root_node)

        # --- LOGIC GATE 1: API IN COMPONENT ---
        # Only flags if API is called directly in the body or useEffect,
        # but NOT if it's just a variable name.
        if len(api_calls) > 0:
            self.issues.append({
                "type": "API_IN_COMPONENT",
                "file": filename,
                "function": api_calls[0],
                "target": "SERVICE",
                "moduleId": module_id
            })

        # --- LOGIC GATE 2: STATE_AND_FETCH_LOGIC ---
        # Flags if there are 2+ states being managed alongside a side-effect.
        # This suggests the component is doing too much 'Orchestration'.
        if len(found_states) >= 2 and len(effect_blocks) > 0:
            self.issues.append({
                "type": "STATE_AND_FETCH_LOGIC",
                "file": filename,
                "moduleId": module_id,
                "target": "HOOK",
                "effects": [f"sync{filename.split('.')[0]}"],
                "states": found_states,
                "handlers": ["handleError", "setLoading"]
            })

    def _manual_function_scan(self, node, source_code, filename):
        def walk(n):
            # Detects Standard functions, Arrow functions, and Class methods
            is_func = False
            name = "anonymous"
            body = None

            if n.type == 'function_declaration':
                name_node = n.child_by_field_name('name')
                body_node = n.child_by_field_name('body')
                if name_node and body_node:
                    name, body, is_func = get_node_text(name_node, source_code), get_node_text(body_node,
                                                                                               source_code), True

            elif n.type == 'variable_declarator':
                val = n.child_by_field_name('value')
                if val and val.type in ['arrow_function', 'function_expression']:
                    name_node = n.child_by_field_name('name')
                    body_node = val.child_by_field_name('body')
                    if name_node and body_node:
                        name, body, is_func = get_node_text(name_node, source_code), get_node_text(body_node,
                                                                                                   source_code), True

            if is_func:
                self._store_function(name, body, filename)

            for child in n.children:
                walk(child)

        walk(node)

    def _store_function(self, name, body, filename):
        # We strip comments and whitespace to ensure the hash is "Semantic"
        # (meaning two functions are duplicates even if the spacing is different)
        clean_body = "".join(body.split())
        body_hash = hashlib.md5(clean_body.encode()).hexdigest()

        if body_hash not in self.function_map:
            self.function_map[body_hash] = {"name": name, "files": set()}
        self.function_map[body_hash]["files"].add(filename)

    def _check_duplicates(self):
        for h, data in self.function_map.items():
            if len(data["files"]) > 1:
                self.issues.append({
                    "type": "DUPLICATE_FUNCTION",
                    "function": data["name"],
                    "files": list(data["files"]),
                    "target": "UTIL"
                })


# Flask Routes (same as before)
@app.route('/')
def index(): return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload_and_analyze():
    file = request.files.get('file')
    if not file: return jsonify({"error": "No file"}), 400
    temp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(temp_dir, "repo.zip")
        file.save(zip_path)
        extract_path = os.path.join(temp_dir, "ext")
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_path)
        result = RepoAnalyzer(extract_path).analyze()
        return jsonify(result)
    finally:
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    app.run(debug=True, port=5000)