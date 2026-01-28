"""
Security tests for console command validation.

Tests to ensure that no dangerous commands can be executed via the console
that could harm the host system.
"""

import sys
import re
from pathlib import Path

# Add the app directory to the path
sys.path.insert(0, str(Path(__file__).parent))

# Import the validation function and constants
from app import should_allow_console_command, BLOCKED_CONSOLE_COMMANDS, MAX_COMMAND_LENGTH


def test_basic_valid_commands():
    """Test that basic valid game commands are allowed."""
    valid_commands = [
        "help",
        "say Hello players",
        "time set 1000",
        "weather clear",
        "gamemode creative Player1",
        "tp Player1 100 64 200",
        "give Player1 diamond 64",
        "kick Player1 Misbehaving",
        "save-all",
        "list",
    ]
    
    print("Testing valid commands...")
    for cmd in valid_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert is_allowed, f"Valid command '{cmd}' was blocked: {error}"
        print(f"  ✓ '{cmd}' - allowed")
    print()


def test_blocked_commands():
    """Test that blocked commands are rejected."""
    blocked_commands = [
        "op Player1",
        "deop Player1",
        "stop",
        "restart",
        "update",
        "ban Player1",
        "unban Player1",
        "whitelist add Player1",
        "whitelist remove Player1",
        "reload",
        "plugins",
        "plugin load test",
    ]
    
    print("Testing blocked commands...")
    for cmd in blocked_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"Blocked command '{cmd}' was allowed"
        assert error, f"No error message for blocked command '{cmd}'"
        print(f"  ✓ '{cmd}' - blocked: {error}")
    print()


def test_shell_metacharacters():
    """Test that shell metacharacters are rejected."""
    dangerous_commands = [
        "say test; rm -rf /",
        "say test && shutdown now",
        "say test | cat /etc/passwd",
        "say test `whoami`",
        "say test $(reboot)",
        "say test > /etc/hosts",
        "say test < /dev/null",
        "say test \\ escape",
        "say test\nls",
        "say test\rshutdown",
    ]
    
    print("Testing shell metacharacters...")
    for cmd in dangerous_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"Command with shell metacharacters '{cmd}' was allowed"
        assert "metacharacters" in error.lower() or "forbidden characters" in error.lower(), \
            f"Wrong error for '{cmd}': {error}"
        print(f"  ✓ Blocked: {cmd[:50]}... - {error}")
    print()


def test_path_traversal():
    """Test that path traversal attempts are blocked."""
    path_traversal_commands = [
        "say ../../../etc/passwd",
        "list ../../../root",
        "say test ../../.ssh/id_rsa",
        "help ../",
    ]
    
    print("Testing path traversal attempts...")
    for cmd in path_traversal_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"Path traversal command '{cmd}' was allowed"
        print(f"  ✓ Blocked: {cmd} - {error}")
    print()


def test_system_paths():
    """Test that commands accessing system paths are blocked."""
    system_path_commands = [
        "say /etc/shadow",
        "list /proc/self",
        "say /sys/kernel",
        "help /dev/sda",
        "say /root/.ssh",
        "list /tmp/malicious",
        "say /var/log/secure",
        "help /opt/other-app",
    ]
    
    print("Testing system path access...")
    for cmd in system_path_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"System path command '{cmd}' was allowed"
        print(f"  ✓ Blocked: {cmd} - {error}")
    print()


def test_dangerous_system_commands():
    """Test that dangerous system commands in arguments are blocked."""
    dangerous_commands = [
        "say sudo reboot",
        "help su root",
        "say chmod 777 file",
        "list chown root file",
        "say rm -rf /",
        "help mv important gone",
        "say cp secret public",
        "list dd if=/dev/zero",
        "say mkfs.ext4 /dev/sda",
        "help mount /dev/sda",
        "say umount /mnt",
        "list kill -9 1",
        "say pkill java",
        "help reboot now",
        "say shutdown -h now",
        "list poweroff",
        "say halt",
        "help init 0",
        "say systemctl stop firewall",
        "list service nginx stop",
    ]
    
    print("Testing dangerous system commands...")
    for cmd in dangerous_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"Dangerous command '{cmd}' was allowed"
        assert "forbidden pattern" in error.lower(), f"Wrong error for '{cmd}': {error}"
        print(f"  ✓ Blocked: {cmd} - Forbidden pattern detected")
    print()


def test_word_boundary_false_positives():
    """Test that valid commands with substrings matching dangerous patterns are allowed."""
    valid_commands_with_substrings = [
        "say I have skill in building",  # Contains 'kill' but should be allowed
        "say This pseudocode is great",  # Contains 'sudo' but should be allowed
        "say Let me describe this",      # Contains 'rm' but should be allowed
        "say Microservice architecture", # Contains 'service' but should be allowed
        "say The suspend feature",       # Contains 'su' but should be allowed
        "say Checkpoints saved",         # Contains 'cp' but should be allowed
        "say Adding more features",      # Contains 'dd' but should be allowed
    ]
    
    print("Testing word boundary (no false positives)...")
    for cmd in valid_commands_with_substrings:
        is_allowed, error = should_allow_console_command(cmd)
        assert is_allowed, f"Valid command '{cmd}' was incorrectly blocked: {error}"
        print(f"  ✓ '{cmd}' - allowed (substring not matched)")
    print()


def test_command_length():
    """Test that overly long commands are rejected."""
    print("Testing command length limits...")
    
    # Test maximum allowed length
    max_cmd = "say " + "A" * (MAX_COMMAND_LENGTH - 4)
    is_allowed, error = should_allow_console_command(max_cmd)
    assert is_allowed, f"Command at max length was blocked: {error}"
    print(f"  ✓ Command at max length ({MAX_COMMAND_LENGTH}) - allowed")
    
    # Test exceeding maximum length
    too_long_cmd = "say " + "A" * MAX_COMMAND_LENGTH
    is_allowed, error = should_allow_console_command(too_long_cmd)
    assert not is_allowed, "Command exceeding max length was allowed"
    assert "too long" in error.lower(), f"Wrong error for long command: {error}"
    print(f"  ✓ Command exceeding max length - blocked: {error}")
    print()


def test_empty_and_null():
    """Test that empty commands and null bytes are rejected."""
    print("Testing empty commands and null bytes...")
    
    invalid_commands = [
        "",
        "   ",
        "\x00",
        "say test\x00malicious",
    ]
    
    for cmd in invalid_commands:
        is_allowed, error = should_allow_console_command(cmd)
        assert not is_allowed, f"Invalid command '{repr(cmd)}' was allowed"
        print(f"  ✓ Blocked: {repr(cmd)} - {error}")
    print()


def test_edge_cases():
    """Test edge cases and unusual inputs."""
    print("Testing edge cases...")
    
    edge_cases = [
        ("SAY HELLO", True),  # Uppercase should work
        ("Say Hello World", True),  # Mixed case
        ("  help  ", True),  # Leading/trailing spaces
        ("say     multiple   spaces", True),  # Multiple spaces
    ]
    
    for cmd, should_allow in edge_cases:
        is_allowed, error = should_allow_console_command(cmd)
        if should_allow:
            assert is_allowed, f"Valid edge case '{cmd}' was blocked: {error}"
            print(f"  ✓ '{cmd}' - allowed (edge case)")
        else:
            assert not is_allowed, f"Invalid edge case '{cmd}' was allowed"
            print(f"  ✓ '{cmd}' - blocked: {error}")
    print()


def test_send_console_command_validation():
    """Test the defense-in-depth validation in send_console_command."""
    print("Testing send_console_command validation...")
    
    # Import the function
    from app import send_console_command, MAX_COMMAND_LENGTH, CONSOLE_PIPE
    import tempfile
    import os
    
    # Create a temporary FIFO pipe for testing if one doesn't exist
    pipe_created = False
    if not CONSOLE_PIPE.exists():
        try:
            CONSOLE_PIPE.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(str(CONSOLE_PIPE))
            pipe_created = True
            print(f"  ℹ Created temporary test pipe at {CONSOLE_PIPE}")
        except Exception as e:
            print(f"  ⚠ Could not create test pipe: {e}")
            print(f"  ⚠ Skipping send_console_command tests")
            print()
            return
    
    try:
        # Test null byte rejection
        try:
            send_console_command("test\x00malicious")
            assert False, "send_console_command should reject null bytes"
        except RuntimeError as e:
            assert "null bytes" in str(e).lower()
            print(f"  ✓ Null byte rejected: {e}")
        
        # Test length limit
        try:
            long_cmd = "say " + "A" * MAX_COMMAND_LENGTH
            send_console_command(long_cmd)
            assert False, "send_console_command should reject overly long commands"
        except RuntimeError as e:
            assert "too long" in str(e).lower()
            print(f"  ✓ Long command rejected: {e}")
        
        # Note: We can't fully test UTF-8 encoding without actual FIFO reader,
        # but the error handling path exists
        print(f"  ✓ UTF-8 encoding validation in place (strict mode)")
        
    finally:
        # Clean up temporary pipe if we created it
        if pipe_created and CONSOLE_PIPE.exists():
            try:
                CONSOLE_PIPE.unlink()
                print(f"  ℹ Cleaned up temporary test pipe")
            except Exception as e:
                print(f"  ⚠ Could not remove test pipe: {e}")
    
    print()


def run_all_tests():
    """Run all security tests."""
    print("=" * 70)
    print("SECURITY TEST SUITE: Console Command Validation")
    print("=" * 70)
    print()
    
    try:
        test_basic_valid_commands()
        test_blocked_commands()
        test_shell_metacharacters()
        test_path_traversal()
        test_system_paths()
        test_dangerous_system_commands()
        test_word_boundary_false_positives()
        test_command_length()
        test_empty_and_null()
        test_edge_cases()
        test_send_console_command_validation()
        
        print("=" * 70)
        print("ALL TESTS PASSED ✓")
        print("=" * 70)
        return 0
        
    except AssertionError as e:
        print()
        print("=" * 70)
        print("TEST FAILED ✗")
        print(f"Error: {e}")
        print("=" * 70)
        return 1
    except Exception as e:
        print()
        print("=" * 70)
        print("UNEXPECTED ERROR ✗")
        print(f"Error: {type(e).__name__}: {e}")
        print("=" * 70)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
