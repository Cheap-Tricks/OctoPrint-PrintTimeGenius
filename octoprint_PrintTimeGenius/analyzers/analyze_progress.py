#!/usr/bin/env python

from __future__ import print_function
import subprocess
import sys
import json
import os
import platform

def main():
  binary_base_name = sys.argv[1]
  machine = platform.machine()
  gcode = sys.argv[2]
  mcodes = None
  if len(sys.argv) > 3:
    mcodes = sys.argv[3]
  cmd = [
      os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])),
                   "{}.{}".format(binary_base_name, machine)),
      gcode]
  if mcodes:
    cmd += [mcodes]
  print("Running: {}".format(" ".join('"{}"'.format(c) for c in cmd)), file=sys.stderr)
  if not os.path.isfile(cmd[0]):
    print("Can't find: {}".format(cmd[0]), file=sys.stderr)
    exit(2)
  if not os.access(cmd[0], os.X_OK):
    print("Not executable: {}".format(cmd[0]), file=sys.stderr)
    exit(3)
  try:
    output = subprocess.check_output(cmd)
  except Exception as e:
    print(e, file=sys.stderr)
    exit(1)
  progress = []
  result = {}
  first_filament = None
  last_filament = None
  max_filament = None
  for line in output.split("\n"):
    if not line:
      continue
    if line.startswith("Progress:"):
      line = line[len("Progress:"):]
      (filepos, filament, time) = map(float, line.split(","))
      if filament > 0 and not first_filament:
        first_filament = filepos
      if not max_filament or filament > max_filament:
        last_filament = filepos
        max_filament = filament
      progress.append([filepos, time])
    elif line.startswith("Analysis:"):
      line = line[len("Analysis:"):]
      result.update(json.loads(line))
  result["firstFilament"] = first_filament
  result["lastFilament"] = last_filament
  total_time = progress[-1][1]
  most_recent_progress = float("-inf")
  result["progress"] = [[0,total_time]]
  for progress_entry in progress:
    if (most_recent_progress+60 < progress_entry[1] or
        progress_entry[0] == first_filament or
        progress_entry[0] == last_filament):
      most_recent_progress = progress_entry[1]
      result["progress"].append(
          [progress_entry[0],
           total_time-progress_entry[1]])
  result["progress"].append([1,0])
  result["estimatedPrintTime"] = total_time
  print(json.dumps(result))
  exit(0)

if __name__ == "__main__":
  main()
