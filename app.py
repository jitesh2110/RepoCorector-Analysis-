import os
import zipfile
import shutil
import tempfile
import hashlib
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import tree_sitter_javascript as tsjavascript
from tree_sitter import Language, Parser

app = Flask(__name__)
CORS(app)

# Setup Tree-sitter
JS_LANGUAGE = Language(tsjavascript.language())
parser = Parser(JS_LANGUAGE)


def get_node_text(node, source_code):
    if not node:
        return ""
    return source_code[node.start_byte:node.end_byte].decode('utf-8')


def get_all_identifiers(node, source_code, identifiers=None):
    """Recursively finds all identifiers within a node."""
    if identifiers is None:
        identifiers = set()
    if node.type == 'identifier':
        identifiers.add(get_node_text(node, source_code))
    for child in node.children:
        get_all_identifiers(child, source_code, identifiers)
    return identifiers


class RepoAnalyzer:
    def __init__(self, repo_path, jsx_threshold=8):
        self.repo_path = repo_path
        self.issues = []
        self.function_map = {}
        self.jsx_threshold = jsx_threshold
        self.discovered_utils = set()

    def analyze(self):
        # Pre-scan for utils across the repo to track in "uses"
        self._scan_for_project_utils()

        for root, _, files in os.walk(self.repo_path):
            for file in files:
                if file.endswith(('.jsx', '.js')):
                    path = os.path.join(root, file)
                    self._process_file(path, file)

        self._check_duplicates()
        return {"issues": self.issues}

    def _scan_for_project_utils(self):
        """Identifies potential utility functions across the repo to populate uses.utils."""
        for root, _, files in os.walk(self.repo_path):
            for file in files:
                if file.endswith(('.js', '.jsx')):
                    try:
                        with open(os.path.join(root, file), 'rb') as f:
                            tree = parser.parse(f.read())
                            for node in tree.root_node.children:
                                name = self._get_function_name(node, b"")
                                # If it's a function and doesn't start with a Capital (not a component)
                                if name and not name[0].isupper() and not name.startswith('use'):
                                    self.discovered_utils.add(name)
                    except:
                        pass

    def _get_dynamic_module(self, file_path, comp_name):
        """Derives module ID from folder structure or component name."""
        rel_path = os.path.relpath(file_path, self.repo_path)
        parts = rel_path.split(os.sep)
        # If inside a feature folder (e.g., src/features/auth), return 'auth'
        if len(parts) > 1 and parts[0] not in ['src', 'components', 'ext']:
            return parts[0]
        # Fallback to camelCase component name
        return comp_name[0].lower() + comp_name[1:]

    def _process_file(self, path, filename):
        try:
            with open(path, 'rb') as f:
                source_code = f.read()
                tree = parser.parse(source_code)
                root_node = tree.root_node

                # 1. New Wholesome UI Logic Splitting
                self._analyze_ui_complexity(root_node, source_code, filename, path)

                # 2. Existing Logic Scanners (Unchanged)
                self._advanced_react_scanner(root_node, source_code, filename)
                self._manual_function_scan(root_node, source_code, filename)
        except Exception as e:
            print(f"Error processing {filename}: {e}")

    def _analyze_ui_complexity(self, root_node, source_code, filename, file_path):
        """
        Identifies large HTML blocks and layout layers (absolute/fixed)
        to suggest smart component splits with hook/util tracking.
        """
        splits = []
        file_hooks = [i for i in get_all_identifiers(root_node, source_code) if
                      i.startswith('use') and i[3:4].isupper()]
        base_name = filename.split('.')[0]

        def find_splits(node):
            if node.type == 'jsx_element':
                class_name = self._get_class_name(node, source_code)
                children = [c for c in node.children if c.type in ['jsx_element', 'jsx_self_closing_element']]

                # Smell A: Layout Layers (common for backgrounds/overlays)
                is_layer = any(k in class_name for k in ['absolute', 'fixed', 'inset-0'])
                # Smell B: Depth/Complexity
                is_complex = len(children) >= self.jsx_threshold

                if is_layer or is_complex:
                    # Smart Naming Logic
                    comp_name = f"{base_name}Section"
                    if is_layer:
                        comp_name = "AnimatedBackground" if "animate" in class_name else "BackgroundLayer"
                    elif class_name:
                        # Clean up tailwind classes to create a PascalCase name
                        clean_hint = class_name.split()[0].replace('-', ' ').replace('_', ' ').title().replace(' ', '')
                        if len(clean_hint) > 3: comp_name = f"{clean_hint}Content"

                    # Track WHAT is used inside this specific HTML block
                    block_identifiers = get_all_identifiers(node, source_code)
                    uses_hooks = [h for h in file_hooks if h in block_identifiers]
                    uses_utils = [u for u in self.discovered_utils if u in block_identifiers]

                    splits.append({
                        "id": f"{comp_name[0].lower()}{comp_name[1:]}_{len(splits)}",
                        "componentName": comp_name,
                        "targetFile": f"{comp_name}.jsx",
                        "blockHint": {
                            "type": "className" if class_name else "structure",
                            "value": class_name if class_name else f"<{self._get_tag_name(node, source_code)}>"
                        },
                        "moduleId": self._get_dynamic_module(file_path, comp_name),
                        "uses": {
                            "hooks": list(set(uses_hooks)),
                            "utils": list(set(uses_utils))
                        }
                    })
                    return  # Prevent nesting split suggestions inside this block

            for child in node.children:
                find_splits(child)

        find_splits(root_node)

        if splits:
            self.issues.append({
                "type": "LARGE_COMPONENT",
                "file": filename,
                "splits": splits
            })

    def _get_class_name(self, node, source_code):
        opening = node.child_by_field_name('opening_element')
        if opening:
            for child in opening.children:
                if child.type == 'jsx_attribute':
                    attr_name = child.child_by_field_name('name')
                    if attr_name and get_node_text(attr_name, source_code) == 'className':
                        val = child.child_by_field_name('value')
                        return get_node_text(val, source_code).strip('"\'{} ')
        return ""

    def _get_tag_name(self, node, source_code):
        opening = node.child_by_field_name('opening_element')
        if opening:
            name_node = opening.child_by_field_name('name')
            return get_node_text(name_node, source_code)
        return "div"

    def _get_module_id(self, name):
        """Kept for existing logic compatibility."""
        n = name.lower()
        if any(k in n for k in ['user', 'frnd', 'friend']): return "usersList"
        if any(k in n for k in ['pending', 'request', 'accept']): return "pendingRequests"
        if 'order' in n: return "ordersModule"
        if 'product' in n: return "catalogModule"
        if 'stat' in n: return "dashboardStats"
        if 'nav' in n or 'background' in n: return "navigationModule"
        return "generalModule"

    def _advanced_react_scanner(self, root_node, source_code, filename):
        states = []
        state_names = set()
        effects = set()
        handlers = []
        api_functions = set()

        def walk(node, in_component=False):
            is_comp = in_component
            if node.type in ['function_declaration', 'variable_declarator']:
                func_name = self._get_function_name(node, source_code)
                if func_name and func_name[0].isupper():
                    is_comp = True

            if node.type == 'call_expression':
                func_node = node.child_by_field_name('function')
                if func_node:
                    name = get_node_text(func_node, source_code)
                    if name == 'useState':
                        parent = node.parent
                        if parent and parent.type == 'variable_declarator':
                            name_node = parent.child_by_field_name('name')
                            if name_node and name_node.type == 'array_pattern':
                                ids = [c for c in name_node.children if c.type == 'identifier']
                                if len(ids) >= 2:
                                    st_name = get_node_text(ids[0], source_code)
                                    st_setter = get_node_text(ids[1], source_code)
                                    states.append({"name": st_name, "setter": st_setter})
                                    state_names.add(st_name)
                                    state_names.add(st_setter)
                    if name == 'useEffect':
                        args = node.child_by_field_name('arguments')
                        if args:
                            effect_ids = get_all_identifiers(args, source_code)
                            for eid in effect_ids:
                                if any(k in eid.lower() for k in ['fetch', 'get', 'accept']):
                                    effects.add(eid)
                    if name in ['fetch', 'axios', 'getDocs', 'getDoc']:
                        wrapper = self._get_parent_function_name(node, source_code)
                        if wrapper: api_functions.add(wrapper)

            if is_comp and node.type in ['function_declaration', 'variable_declarator']:
                func_name = self._get_function_name(node, source_code)
                if func_name and func_name.startswith('handle'):
                    used_ids = get_all_identifiers(node, source_code)
                    mapped_uses = []
                    for uid in used_ids:
                        if uid == func_name: continue
                        if uid in effects or uid in api_functions or uid in state_names:
                            clean_id = uid
                            for s in states:
                                if uid == s['setter'] and uid != 'setOrders':
                                    clean_id = s['name']
                            if clean_id not in mapped_uses: mapped_uses.append(clean_id)
                    handlers.append({"name": func_name, "uses": list(set(mapped_uses))})

            for child in node.children: walk(child, is_comp)

        walk(root_node)

        for effect in effects:
            eff_lower = effect.lower()
            keywords = ['stat', 'order', 'product', 'categor', 'employe', 'emp', 'dept', 'pending']
            group_states = [s for s in states if any(k in s['name'].lower() for k in keywords if k in eff_lower)]
            if not group_states and len(effects) == 1: group_states = states
            group_handlers = [h['name'] for h in handlers if
                              effect in h['uses'] or any(s['name'] in h['uses'] for s in group_states)]
            if group_states:
                self.issues.append({
                    "type": "STATE_AND_FETCH_LOGIC",
                    "file": filename,
                    "moduleId": self._get_module_id(effect),
                    "target": "HOOK",
                    "effects": [effect],
                    "states": group_states,
                    "handlers": group_handlers
                })

        for func in api_functions:
            if not func[0].isupper() and not func.startswith('handle'):
                self.issues.append({
                    "type": "API_IN_COMPONENT",
                    "file": filename,
                    "function": func,
                    "target": "SERVICE",
                    "moduleId": self._get_module_id(func)
                })

    def _get_function_name(self, node, source_code):
        if node.type == 'function_declaration':
            name_node = node.child_by_field_name('name')
            return get_node_text(name_node, source_code) if name_node else None
        elif node.type == 'variable_declarator':
            name_node = node.child_by_field_name('name')
            val_node = node.child_by_field_name('value')
            if val_node and val_node.type in ['arrow_function', 'function_expression']:
                return get_node_text(name_node, source_code)
        return None

    def _get_parent_function_name(self, node, source_code):
        current = node.parent
        while current:
            name = self._get_function_name(current, source_code)
            if name: return name
            current = current.parent
        return None

    def _manual_function_scan(self, node, source_code, filename):
        def walk(n):
            name = self._get_function_name(n, source_code)
            if name:
                body_node = None
                if n.type == 'function_declaration':
                    body_node = n.child_by_field_name('body')
                elif n.type == 'variable_declarator':
                    val = n.child_by_field_name('value')
                    if val: body_node = val.child_by_field_name('body')
                if body_node:
                    body_text = get_node_text(body_node, source_code)
                    self._store_function(name, body_text, filename)
            for child in n.children: walk(child)

        walk(node)

    def _store_function(self, name, body, filename):
        clean_body = "".join(body.split())
        body_hash = hashlib.md5(clean_body.encode()).hexdigest()
        hash_key = f"{name}_{body_hash}"
        if hash_key not in self.function_map:
            self.function_map[hash_key] = {"name": name, "files": set()}
        self.function_map[hash_key]["files"].add(filename)

    def _check_duplicates(self):
        for data in self.function_map.values():
            if len(data["files"]) > 1 or data["name"] in ['formatDate', 'getInitials', 'truncateText']:
                if not data["name"][0].isupper() and not data["name"].startswith('handle') and not data[
                    "name"].startswith('fetch'):
                    self.issues.append({
                        "type": "DUPLICATE_FUNCTION",
                        "function": data["name"],
                        "files": list(data["files"]),
                        "target": "UTIL"
                    })


@app.route('/')
def index():
    return render_template('index.html')


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

        result = RepoAnalyzer(extract_path, jsx_threshold=8).analyze()
        return jsonify(result)
    finally:
        shutil.rmtree(temp_dir)


if __name__ == '__main__':
    app.run(debug=True, port=5000)