# coding=utf-8
from __future__ import absolute_import
from __future__ import division

import octoprint.plugin
import octoprint.filemanager.storage
from octoprint.printer.estimation import PrintTimeEstimator
from octoprint.filemanager.analysis import GcodeAnalysisQueue
import logging
import bisect
import subprocess
import json
import shlex
import time
import os
import sys

def _interpolate(l, point):
  """Use the point value to interpolate a new value from the list.
  l must be a sorted list of lists.  point is a value to interpolate.
  Return None if the point is out of range.
  If the result is not None, return interpolated array."""
  # ge is the index of the first element >= point
  if point < l[0][0] or point > l[-1][0]:
    return None
  if point == l[0][0]:
    return l[0]
  if point == l[-1][0]:
    return l[-1]
  right_index = bisect.bisect_right(l, [point])
  left_index = right_index - 1
  # ratio 0 means use the left_index one, 1 means the right_index one
  ratio = (point - l[left_index][0])/(l[right_index][0] - l[left_index][0])
  return [x[0]*(1-ratio) + x[1]*ratio
          for x in zip(l[left_index], l[right_index])]

class GeniusEstimator(PrintTimeEstimator):
  """Uses previous generated analysis to estimate print time remaining."""

  def __init__(self, job_type, printer, file_manager, logger, current_history):
    super(GeniusEstimator, self).__init__(job_type)
    self._path = printer.get_current_job()["file"]["path"]
    self._origin = printer.get_current_job()["file"]["origin"]
    self._file_manager = file_manager
    self._logger = logger
    self._current_history = current_history
    self._current_progress_index = -1 # Points to the entry that we used for remaining time
    self._current_total_printTime = None # When we started using the current_progress
    self._called_genius_yet = False

  def _genius_estimate(self, progress, printTime, cleanedPrintTime, statisticalTotalPrintTime, statisticalTotalPrintTimeType):
    """Return an estimate for the total print time remaining.
    Returns (remaining_time_in_seconds, "genius") or None if it failed.
    """
    # The progress is a sorted list of pairs [filepos, remaining_time].
    # It maps from filepos to estimated remaining time.
    # filepos is between 0 and 1, same as progress.
    # actual progress is in seconds
    if not self._called_genius_yet:
      # Pretend like the first call is always at progress 0
      progress = 0
      self._called_genius_yet = True
    metadata = self._file_manager.get_metadata(self._origin, self._path)
    if not metadata:
      return None
    if not "analysis" in metadata or not "progress" in metadata["analysis"]:
      return None
    filepos_to_progress = metadata["analysis"]["progress"]
    # Can we increment the current_progress_index?
    new_progress_index = self._current_progress_index
    while (new_progress_index + 1 < len(filepos_to_progress) and
           progress >= filepos_to_progress[new_progress_index+1][0]):
      new_progress_index += 1 # Increment
    if new_progress_index < 0:
      return None # We're not even in range yet.
    if new_progress_index != self._current_progress_index:
      # We advanced to a new index, let's make new estimates.
      if (progress > metadata["analysis"]["firstFilament"] and
          not "firstFilamentPrintTime" in self._current_history):
        self._current_history["firstFilamentPrintTime"] = printTime
      if (not "lastFilamentPrintTime" in self._current_history or
          progress <= metadata["analysis"]["lastFilament"]):
        self._current_history["lastFilamentPrintTime"] = printTime
      interpolation = _interpolate(filepos_to_progress, progress)
      if not interpolation:
        return None
      # This is our best guess for the total print time.
      self._current_total_printTime = interpolation[1] + printTime
      self._current_progress_index = new_progress_index
    remaining_print_time = self._current_total_printTime - printTime
    return remaining_print_time, "genius"

  def estimate(self, progress, printTime, cleanedPrintTime, statisticalTotalPrintTime, statisticalTotalPrintTimeType):
    default_result = super(GeniusEstimator, self).estimate(
        progress, printTime, cleanedPrintTime,
        statisticalTotalPrintTime, statisticalTotalPrintTimeType)
    result = default_result # This is the result that we will report.
    genius_result = default_result # Genius result defaults to the default_result
    try:
      genius_result = self._genius_estimate(
          progress, printTime, cleanedPrintTime,
          statisticalTotalPrintTime, statisticalTotalPrintTimeType)
      if genius_result:
        result = genius_result # If genius worked, use it.
    except Exception as e:
      self._logger.warning("Failed to estimate, ignoring.", exc_info=e)
    if not default_result:
      default_result = (0, None)
    if not genius_result:
      genius_result = (0, None)
    self._logger.debug(", " + ", ".join(map(str, [printTime, default_result[0], default_result[1], genius_result[0], genius_result[1], progress])))
    return result

class GeniusAnalysisQueue(GcodeAnalysisQueue):
  """Generate an analysis to use for printing time remaining later."""
  def __init__(self, finished_callback, plugin):
    super(GeniusAnalysisQueue, self).__init__(finished_callback)
    self._plugin = plugin

  def compensate_analysis(self, analysis):
    logger = self._plugin._logger
    """Compensate for the analyzed print time by looking at previous statistics of
    how long it took to heat up or cool down.
    Modifies the analysis in place.
    """
    try:
      if not self._plugin._settings.has(["print_history"]):
        return
      print_history = self._plugin._settings.get(["print_history"])
      if not print_history:
        return
      # How long did it take to heat up on previous prints?
      logging.info("Gathering compensation data...")
      heat_up_times = [ph["firstFilamentPrintTime"]
                       for ph in print_history]
      logger.info("Recent heat-up times in seconds: {}".format(", ".join(map(str, heat_up_times))))
      average_heat_up_time = sum(heat_up_times) / len(heat_up_times)
      logger.info("Average heat-up: {} seconds".format(average_heat_up_time))
      # How long did it take to cool down on previous prints?
      cool_down_times = [ph["payload"]["time"] - ph["lastFilamentPrintTime"]
                         for ph in print_history]
      logger.info("Recent cool-down times in seconds: {}".format(", ".join(map(str, cool_down_times))))
      average_cool_down_time = sum(cool_down_times) / len(cool_down_times)
      logger.info("Average cool-down: {} seconds".format(average_cool_down_time))
      # Factor from the time actual time spent extruding to the predicted.
      logger.info("Time spent printing, actual vs predicted: {}".format(
          ", ".join("{}/{}".format(ph["lastFilamentPrintTime"] - ph["firstFilamentPrintTime"],
                                   ph["analysisLastFilamentPrintTime"] - ph["analysisFirstFilamentPrintTime"])
                    for ph in print_history)))
      print_time_factor = [(ph["lastFilamentPrintTime"] - ph["firstFilamentPrintTime"]) /
                           (ph["analysisLastFilamentPrintTime"] - ph["analysisFirstFilamentPrintTime"])
                           for ph in print_history]
      average_print_time_factor = sum(print_time_factor) / len(print_time_factor)
      logger.info("Average scaling factor: {}".format(average_print_time_factor))
      # Now make a new progress map.
      new_progress = []
      last_filament_remaining_time = _interpolate(analysis["progress"],
                                                  analysis["lastFilament"])[1]
      for p in analysis["progress"]:
        if p[0] < analysis["firstFilament"]:
          continue # Ignore anything before the first filament.
        if p[0] >= analysis["lastFilament"]:
          break # Don't add estimates from the cooldown
        remaining_time = p[1] # Starting value.
        remaining_time -= last_filament_remaining_time # Remove expected cooldown.
        remaining_time *= average_print_time_factor # Compensate for scale.
        remaining_time += average_cool_down_time # Add in average cooldown
        new_progress.append([p[0], remaining_time])
      new_progress.insert(0, [0, new_progress[0][1] + average_heat_up_time])
      new_progress.append([1,0])
      analysis["progress"] = new_progress
      analysis["estimatedPrintTime"] = new_progress[0][1]
    except Exception as e:
      logger.warning("Failed to compensate", exc_info=e)

  def _do_analysis(self, high_priority=False):
    logger = self._plugin._logger
    results = None
    if self._plugin._settings.get(["enableOctoPrintAnalyzer"]):
      logger.info("Running built-in analysis.")
      results = super(GeniusAnalysisQueue, self)._do_analysis(high_priority)
      logger.info("Result: {}".format(results))
      self._finished_callback(self._current, results)
    else:
      logger.info("Not running built-in analysis.")
    for analyzer in self._plugin._settings.get(["analyzers"]):
      command = analyzer["command"].format(gcode=self._current.absolute_path)
      if not analyzer["enabled"]:
        logger.info("Disabled: {}".format(command))
        continue
      logger.info("Running: {}".format(command))
      try:
        results_text = subprocess.check_output(shlex.split(command))
        logger.info("Result: {}".format(results_text))
        new_results = json.loads(results_text)
        results.update(new_results)
        logger.info("Merged result: {}".format(results))
        self._finished_callback(self._current, results)
      except Exception as e:
        logger.warning("Failed to run '{}'".format(command), exc_info=e)
    # Before we potentially modify the result from analysis, save them.
    try:
      if not all(x in results
                 for x in ["progress",
                           "firstFilament",
                           "lastFilament"]):
        return results
      results["analysisPrintTime"] = results["estimatedPrintTime"]
      results["analysisFirstFilamentPrintTime"] = (
          results["analysisPrintTime"] - _interpolate(
              results["progress"],
              results["firstFilament"])[1])
      results["analysisLastFilamentPrintTime"] = (
          results["analysisPrintTime"] - _interpolate(
              results["progress"],
              results["lastFilament"])[1])
      self.compensate_analysis(results) # Adjust based on history
      logger.info("Compensated result: {}".format(results))
    except Exception as e:
      logger.warning("Failed to compensate", exc_info=e)
    return results

class PrintTimeGeniusPlugin(octoprint.plugin.SettingsPlugin,
                            octoprint.plugin.AssetPlugin,
                            octoprint.plugin.TemplatePlugin,
                            octoprint.plugin.StartupPlugin,
                            octoprint.plugin.EventHandlerPlugin,
                            octoprint.plugin.BlueprintPlugin):
  def __init__(self):
    self._logger = logging.getLogger(__name__)
    self._current_history = None
  ##~~ SettingsPlugin mixin

  def get_settings_defaults(self):
    built_in_analyzers = [["analyzers/analyze_gcode_comments.py"],
                          ["analyzers/analyze_progress.py", "marlin-calc"]]
    current_path = os.path.dirname(os.path.realpath(__file__))
    return {
        "analyzers": [
            {"command": '{python} {analyzer} "{{gcode}}"'.format(
                python = sys.executable,
                analyzer = " ".join([os.path.join(current_path, x[0])] + x[1:])),
             "enabled": True}
            for x in built_in_analyzers],
        "exactDurations": True,
        "enableOctoPrintAnalyzer": True,
        "print_history": []
    }

  ##~~ EventHandlerPlugin API
  def on_event(self, event, payload):
    """Record how long print's actually took when they succeed.

    We want to record how long it took to finish this print so that we can make
    future estimates more accurate using linear regression.
    """
    if event == "PrintDone":
      # Store the details and also the timestamp.
      if not self._settings.has(["print_history"]):
        print_history = []
      else:
        print_history = self._settings.get(["print_history"])
      metadata = self._file_manager.get_metadata(payload["origin"], payload["path"])
      if not "analysis" in metadata or not "analysisPrintTime" in metadata["analysis"]:
        return
      self._current_history["payload"] = payload
      self._current_history["timestamp"] = time.time()
      for x in ("analysisPrintTime",
                "analysisFirstFilamentPrintTime",
                "analysisLastFilamentPrintTime"):
        self._current_history[x] = metadata["analysis"][x]
      print_history.append(self._current_history)
      self._current_history = None
      print_history.sort(key=lambda x: x["timestamp"], reverse=True)
      MAX_HISTORY_ITEMS = 5
      del print_history[MAX_HISTORY_ITEMS:]
      self._settings.set(["print_history"], print_history)
      self._settings.save() # This might also save settings that we didn't intend to save...

  @octoprint.plugin.BlueprintPlugin.route("/analyze/<origin>/<path:path>", methods=["GET"])
  @octoprint.plugin.BlueprintPlugin.route("/analyse/<origin>/<path:path>", methods=["GET"])
  def analyze_file(self, origin, path):
    """Add a file to the analysis queue."""
    queue_entry = self._file_manager._analysis_queue_entry(origin, path)
    if queue_entry is None:
      return ""
    results = self._file_manager.analyse(origin, path)
    return ""

  ##~~ StartupPlugin API

  def on_startup(self, host, port):
    # setup our custom logger
    from octoprint.logging.handlers import CleaningTimedRotatingFileHandler
    logging_handler = CleaningTimedRotatingFileHandler(self._settings.get_plugin_logfile_path(postfix="engine"), when="D", backupCount=3)
    logging_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logging_handler.setLevel(logging.DEBUG)

    self._logger.addHandler(logging_handler)
    self._logger.propagate = False
    # TODO: Remove the below after https://github.com/foosel/OctoPrint/pull/2723 is merged.
    self._file_manager.original_add_file = self._file_manager.add_file
    def new_add_file(destination, path, file_object, links=None, allow_overwrite=False, printer_profile=None, analysis=None, display=None):
      return self._file_manager.original_add_file(destination, path, file_object, links, allow_overwrite, printer_profile, None, display)
    self._file_manager.add_file = new_add_file



  ##~~ AssetPlugin mixin

  def get_assets(self):
    # Define your plugin's asset files to automatically include in the
    # core UI here.
    return dict(
	js=["js/PrintTimeGenius.js"],
	css=["css/PrintTimeGenius.css"],
	less=["less/PrintTimeGenius.less"]
    )

  ##~~ Gcode Analysis Hook
  def custom_gcode_analysis_queue(self, *args, **kwargs):
    return dict(gcode=lambda finished_callback: GeniusAnalysisQueue(
        finished_callback, self))
  def custom_estimation_factory(self, *args, **kwargs):
    def make_genius_estimator(job_type):
      self._current_history = {}
      return GeniusEstimator(
          job_type, self._printer, self._file_manager, self._logger, self._current_history)
    return make_genius_estimator

  ##~~ Softwareupdate hook

  def get_update_information(self):
    # Define the configuration for your plugin to use with the Software Update
    # Plugin here. See https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
    # for details.
    return dict(
	PrintTimeGenius=dict(
	    displayName="Print Time Genius Plugin",
	    displayVersion=self._plugin_version,

	    # version check: github repository
	    type="github_release",
	    user="eyal0",
	    repo="OctoPrint-PrintTimeGenius",
	    current=self._plugin_version,

	    # update method: pip
	    pip="https://github.com/eyal0/OctoPrint-PrintTimeGenius/archive/{target_version}.zip"
	)
    )


# If you want your plugin to be registered within OctoPrint under a different name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here. Same goes for the other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties. See the documentation for that.
__plugin_name__ = "PrintTimeGenius Plugin"

def __plugin_load__():
  global __plugin_implementation__
  __plugin_implementation__ = PrintTimeGeniusPlugin()

  global __plugin_hooks__
  __plugin_hooks__ = {
      "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
      "octoprint.filemanager.analysis.factory": __plugin_implementation__.custom_gcode_analysis_queue,
      "octoprint.printer.estimation.factory": __plugin_implementation__.custom_estimation_factory
  }