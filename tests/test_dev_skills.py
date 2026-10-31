"""Tests for Jarvis codebase self-engineering and developer tools."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jarvis.skills.dev_shell
from jarvis.core.security_boundaries import DualKeyApprovalManager
from jarvis.skills.base import SkillRegistry


def test_dev_skills():
    mgr = DualKeyApprovalManager.get_instance()
    mgr.set_approval_handler(lambda req: True)
    try:
        tools = SkillRegistry.get_all_tools()
        print(f"Total registered tools: {len(tools)}")

        assert "execute_powershell" in tools
        assert "list_directory" in tools
        assert "read_file" in tools
        assert "write_file" in tools
        assert "patch_file" in tools
        assert "search_codebase" in tools
        assert "run_project_tests" in tools
        assert "git_status" in tools
        assert "read_multiple_files" in tools

        print("\n1. Testing list_directory...")
        dir_res = SkillRegistry.execute("list_directory", folder_path=".")
        print("Directory listing excerpt:\n" + "\n".join(dir_res.splitlines()[:5]))
        assert "run.py" in dir_res

        print("\n2. Testing read_file with line numbers...")
        read_res = SkillRegistry.execute("read_file", file_path="run.py", start_line=1, line_count=8)
        print("Read excerpt:\n" + read_res)
        assert "run.py - Lines 1 to 8" in read_res
        assert "1:" in read_res

        print("\n2b. Testing read_multiple_files (batch)...")
        batch_res = SkillRegistry.execute("read_multiple_files", file_paths="run.py, jarvis/audio/stt.py", max_lines_per_file=10)
        print("Batch excerpt:\n" + "\n".join(batch_res.splitlines()[:10]))
        assert "run.py" in batch_res
        assert "stt.py" in batch_res

        print("\n3. Testing search_codebase...")
        search_res = SkillRegistry.execute("search_codebase", query="Kokoro", folder_path="jarvis", file_pattern="*.py")
        print("Search excerpt:\n" + "\n".join(search_res.splitlines()[:5]))
        assert "tts.py" in search_res

        print("\n4. Testing write_file & patch_file...")
        scratch_file = "data/scratch_self_test.txt"
        w_res = SkillRegistry.execute("write_file", file_path=scratch_file, content="Jarvis is Sir's AI assistant.\nVersion: 1.0\nStatus: Online\n", overwrite=True)
        print("Write result:", w_res)
        assert "Successfully wrote" in w_res

        p_res = SkillRegistry.execute("patch_file", file_path=scratch_file, target_snippet="Version: 1.0", replacement_snippet="Version: 2.0 (Self-Builder)")
        print("Patch result:", p_res)
        assert "Successfully patched" in p_res

        verify_read = SkillRegistry.execute("read_file", file_path=scratch_file, start_line=1, line_count=5)
        print("Patched file content:\n" + verify_read)
        assert "Version: 2.0 (Self-Builder)" in verify_read

        # Clean up scratch test file and its backup
        scratch_path = Path(__file__).resolve().parent.parent / scratch_file
        if scratch_path.exists():
            scratch_path.unlink()
        bak_path = scratch_path.with_suffix(scratch_path.suffix + ".bak")
        if bak_path.exists():
            bak_path.unlink()

        print("\n5. Testing git_status...")
        git_res = SkillRegistry.execute("git_status")
        print("Git status:\n" + git_res)
        assert "Branch:" in git_res

        print("\nALL SELF-ENGINEERING & CODEBASE DEVELOPER SKILLS PASSED CLEANLY!")
    finally:
        mgr.set_approval_handler(None)


if __name__ == "__main__":
    test_dev_skills()
