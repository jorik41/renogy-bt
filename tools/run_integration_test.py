#!/usr/bin/env python3
"""
Integration test runner for ESPHome API test suite
Runs mock server and tests against it
"""
import subprocess
import sys
import time
import signal

def run_integration_test():
    """Run integration test with mock server"""
    print("=" * 70)
    print("ESPHome API Integration Test")
    print("=" * 70)
    
    # Start mock server
    print("\n1. Starting mock ESPHome server on port 6055...")
    server = subprocess.Popen(
        [sys.executable, 'tools/mock_esphome_server.py', '--port', '6055'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to start
    time.sleep(2)
    
    try:
        # Check if server started successfully
        if server.poll() is not None:
            stdout, stderr = server.communicate()
            print(f"✗ Server failed to start:")
            print(stdout)
            print(stderr)
            return False
        
        print("✓ Mock server started successfully")
        
        # Run test suite
        print("\n2. Running comprehensive test suite...")
        print("-" * 70)
        
        test = subprocess.run(
            [sys.executable, 'tools/comprehensive_esphome_test.py', 
             '--port', '6055', '--timeout', '5'],
            capture_output=True,
            text=True
        )
        
        print(test.stdout)
        if test.stderr:
            print("STDERR:", test.stderr)
        
        # Check test results
        if test.returncode == 0:
            print("\n" + "=" * 70)
            print("✓ All integration tests PASSED")
            print("=" * 70)
            return True
        else:
            print("\n" + "=" * 70)
            print("✗ Some integration tests FAILED")
            print("=" * 70)
            return False
            
    finally:
        # Clean up
        print("\n3. Stopping mock server...")
        server.terminate()
        try:
            server.wait(timeout=5)
            print("✓ Mock server stopped")
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
            print("⚠ Mock server killed (timeout)")

if __name__ == "__main__":
    try:
        success = run_integration_test()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\n✗ Integration test error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
