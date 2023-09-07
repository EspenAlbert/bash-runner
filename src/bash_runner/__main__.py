import sys

from bash_runner import run_and_wait

_, *script_args = sys.argv
run_and_wait(" ".join(script_args))
