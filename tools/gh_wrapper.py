#!/usr/bin/env python3
import subprocess
import sys

# The deprecation warning that causes gh to return exit code 1
DEPRECATION_WARNING = "Projects (classic) is being deprecated"

def run_gh(args):
    """Executes gh and returns (returncode, stdout, stderr)."""
    if not args:
        return 1, "", "Usage: gh_wrapper.py <gh-command> [args...]\n"

    cmd = ["gh"] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )
        
        # Handle deprecation warning
        if result.returncode == 1 and result.stderr and DEPRECATION_WARNING in result.stderr:
            return 0, result.stdout, result.stderr
        
        return result.returncode, result.stdout, result.stderr

    except FileNotFoundError:
        return 1, "", "Error: 'gh' CLI not found in PATH\n"
    except Exception as e:
        return 1, "", f"Unexpected error: {e}\n"

def main():
    args = sys.argv[1:]
    returncode, stdout, stderr = run_gh(args)
    
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
        
    sys.exit(returncode)

if __name__ == "__main__":
    main()
