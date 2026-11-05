,
uv venv
.venv\Scripts\Activate.ps1

uv pip install -e .

python -c "import orphic; print(orphic.__file__)"

ls .venv\Lib\site-packages\*.pth
cat .venv\Lib\site-packages\_orphic.pth
python -c "import sys; print(*sys.path, sep='\n')"

deactivate
python -c "import orphic"
.venv\Scripts\Activate.ps1

## Day 01 Quiz Answers - Lesson 2: Editable Installs & Virtual Envs

1. **What does `-e` (editable) actually do?**
   - Instead of copying package files into `site-packages`, an editable install writes a `.pth` (path) file or executable loader inside `site-packages` that points directly to the project's source directory (`src/`). When Python boots up, it reads `.pth` files and appends the source directory to `sys.path`.

2. **Why did step 5 work without reinstalling?**
   - Because Python is importing directly from the source code residing in `src/orphic/__init__.py` through the `.pth` path reference. Any edits to source files are immediately evaluated on the next import execution without needing to rebuild or reinstall the package.

3. **Why use a virtual environment instead of installing into your system Python?**
   - **Isolation and Dependency Control:** Virtual environments isolate dependencies per project, preventing version conflicts between different projects and avoiding accidental modification or corruption of system-level Python packages.

4. **What's the difference between `[build-system] requires` and `[project] dependencies`?**
   - `[build-system] requires`: Specifies the tooling needed **to build and install the package** itself (e.g., `hatchling`, `setuptools`, `flit`).
   - `[project] dependencies`: Specifies the runtime libraries that **your application needs to run** after installation (e.g., `requests`, `pydantic`).

5. **Why is `dependencies` empty?**
   - The `orphic` library currently relies solely on Python's standard library and does not require third-party packages to execute its code.
