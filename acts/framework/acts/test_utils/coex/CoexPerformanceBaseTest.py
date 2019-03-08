#!/usr/bin/env python3
#
# Copyright (C) 2018 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import json
import os
import time
from collections import defaultdict
from collections import OrderedDict
from acts.test_utils.bt.bt_test_utils import disable_bluetooth
from acts.test_utils.coex.CoexBaseTest import CoexBaseTest
from acts.test_utils.coex.coex_test_utils import bokeh_chart_plot
from acts.test_utils.coex.coex_test_utils import (
    collect_bluetooth_manager_dumpsys_logs)
from acts.test_utils.coex.coex_test_utils import multithread_func
from acts.test_utils.coex.coex_test_utils import wifi_connection_check
from acts.test_utils.wifi.wifi_test_utils import wifi_connect
from acts.test_utils.wifi.wifi_test_utils import wifi_test_device_init


def get_atten_range(start, stop, step):
    """Function to derive attenuation range for tests.

    Args:
        start: Start attenuation value.
        stop: Stop attenuation value.
        step: Step attenuation value.

    Returns:
        list of attenuation range.
    """
    atten_step = int(round((stop - start) / float(step)))
    atten_range = [start + x * step for x in range(0, atten_step)]
    return atten_range


class CoexPerformanceBaseTest(CoexBaseTest):
    """Base test class for performance tests.

    Attributes:
        rvr : Dict to save attenuation, throughput, fixed_attenuation.
        a2dp_streaming : Used to denote a2dp test cases.
    """

    def __init__(self, controllers):
        super().__init__(controllers)
        self.a2dp_streaming = False
        self.rvr = {}

    def setup_class(self):
        req_params = ["test_params", "Attenuator"]
        self.unpack_userparams(req_params)
        if hasattr(self, "Attenuator"):
            self.num_atten = self.attenuators[0].instrument.num_atten
        else:
            self.log.error("Attenuator should be connected to run tests.")
            return False
        for i in range(self.num_atten):
            self.attenuators[i].set_atten(0)
        super().setup_class()
        if "performance_result_path" in self.user_params["test_params"]:
            self.performance_files_list = [
                os.path.join(self.test_params["performance_result_path"],
                             files) for files in os.listdir(
                                 self.test_params["performance_result_path"])
            ]
        self.bt_atten_range = get_atten_range(
                            self.test_params["bt_atten_start"],
                            self.test_params["bt_atten_stop"],
                            self.test_params["bt_atten_step"])
        self.wifi_atten_range = get_atten_range(
                            self.test_params["attenuation_start"],
                            self.test_params["attenuation_stop"],
                            self.test_params["attenuation_step"])

    def setup_test(self):
        if "a2dp_streaming" in self.current_test_name:
            self.a2dp_streaming = True
        for i in range(self.num_atten):
            self.attenuators[i].set_atten(0)
        if not wifi_connection_check(self.pri_ad, self.network["SSID"]):
            wifi_connect(self.pri_ad, self.network, num_of_tries=5)
        super().setup_test()

    def teardown_test(self):
        self.performance_baseline_check()
        for i in range(self.num_atten):
            self.attenuators[i].set_atten(0)
            current_atten = int(self.attenuators[i].get_atten())
            self.log.debug(
                "Setting attenuation to zero : Current atten {} : {}".format(
                    self.attenuators[i], current_atten))
        self.a2dp_streaming = False
        if not disable_bluetooth(self.pri_ad.droid):
            self.log.info("Failed to disable bluetooth")
            return False
        self.destroy_android_and_relay_object()
        self.rvr = {}

    def teardown_class(self):
        self.reset_wifi_and_store_results()

    def set_attenuation_and_run_iperf(self, called_func=None):
        """Sets attenuation and runs iperf for Attenuation max value.

        Args:
            called_func : Function object to run.

        Returns:
            True if Pass
            False if Fail
        """
        self.attenuators[self.num_atten - 1].set_atten(0)
        self.rvr["bt_attenuation"] = []
        self.rvr["test_name"] = self.current_test_name
        self.rvr["bt_gap_analysis"] = {}
        self.rvr["bt_range"] = {}
        for bt_atten in self.bt_atten_range:
            self.rvr[bt_atten] = {}
            self.rvr[bt_atten]["fixed_attenuation"] = (
                self.test_params["fixed_attenuation"][str(self.network["channel"])])
            self.log.info("Setting bt attenuation = {}".format(bt_atten))
            self.attenuators[self.num_atten - 1].set_atten(bt_atten)
            for i in range(self.num_atten - 1):
                self.attenuators[i].set_atten(0)
            if not wifi_connection_check(self.pri_ad, self.network["SSID"]):
                wifi_test_device_init(self.pri_ad)
                wifi_connect(self.pri_ad, self.network, num_of_tries=5)
            (self.rvr[bt_atten]["throughput_received"],
                 self.rvr[bt_atten]["a2dp_packet_drop"]) = (
                     self.rvr_throughput(bt_atten, called_func))
            if self.a2dp_streaming:
                if not any(x > 0 for x in self.a2dp_dropped_list):
                    self.rvr[bt_atten]["a2dp_packet_drop"] = []
        return True

    def rvr_throughput(self, bt_atten, called_func=None):
        """Sets attenuation and runs the function passed.

        Args:
            bt_atten: Bluetooth attenuation.
            called_func: Functions object to run parallely.

        Returns:
            Throughput.
        """
        self.iperf_received = []
        self.iperf_variables.received = []
        self.a2dp_dropped_list = []
        self.rvr["bt_attenuation"].append(bt_atten)
        self.rvr[bt_atten]["audio_artifacts"] = {}
        self.rvr[bt_atten]["attenuation"] = []
        self.rvr["bt_gap_analysis"][bt_atten] = {}
        self.rvr["bt_range"][bt_atten] = []
        for atten in self.wifi_atten_range:
            self.rvr[bt_atten]["attenuation"].append(
                atten + self.rvr[bt_atten]["fixed_attenuation"])
            self.log.info("Setting attenuation = {}".format(atten))
            for i in range(self.num_atten - 1):
                self.attenuators[i].set_atten(atten)
            if not wifi_connection_check(self.pri_ad, self.network["SSID"]):
                return self.iperf_received, self.a2dp_dropped_list
            time.sleep(5)  # Time for attenuation to set.
            if called_func:
                if not multithread_func(self.log, called_func):
                    self.teardown_result()
                    return self.iperf_received, self.a2dp_dropped_list
            else:
                self.run_iperf_and_get_result()
            if self.a2dp_streaming:
                analysis_path = self.audio.audio_quality_analysis(self.log_path)
                with open(analysis_path) as f:
                    self.rvr[bt_atten]["audio_artifacts"][atten] = f.readline()
                content = json.loads(self.rvr[bt_atten]["audio_artifacts"][atten])
                self.rvr["bt_gap_analysis"][bt_atten][atten] = {}
                for idx, data in enumerate(content["quality_result"]):
                    if data['artifacts']['delay_during_playback']:
                        self.rvr["bt_gap_analysis"][bt_atten][atten][idx] = (
                                data['artifacts']['delay_during_playback'])
                        self.rvr["bt_range"][bt_atten].append(atten)
                    else:
                        self.rvr["bt_gap_analysis"][bt_atten][atten][idx] = 0
                file_path = collect_bluetooth_manager_dumpsys_logs(
                    self.pri_ad, self.current_test_name)
                self.a2dp_dropped_list.append(
                    self.a2dp_dumpsys.parse(file_path))
            self.teardown_result()
            self.iperf_received.append(
                    float(str(self.iperf_variables.received[-1]).strip("Mb/s")))
        for i in range(self.num_atten - 1):
            self.attenuators[i].set_atten(0)
        return self.iperf_received, self.a2dp_dropped_list

    def performance_baseline_check(self):
        """Checks for performance_result_path in config. If present, plots
        comparision chart else plot chart for that particular test run.

        Returns:
            True if success, False otherwise.
        """
        if self.rvr:
            with open(self.json_file, 'a') as results_file:
                json.dump(OrderedDict(sorted(self.rvr.items(), key=str)),
                          results_file, indent=4)
            self.plot_graph_for_attenuation()
            self.throughput_pass_fail_check()
        else:
            self.log.error("Throughput dict empty!")
            return False
        return True

    def plot_graph_for_attenuation(self):
        """Plots graph and add as JSON formatted results for attenuation with
        respect to its iperf values. Compares rvr results with baseline
        values by calculating throughput limits.
        """
        data_sets = defaultdict(dict)
        test_name = self.current_test_name
        x_label = 'WIFI Attenuation (dB)'
        y_label = []
        legends = defaultdict(list)
        fig_property = {
            "title": test_name,
            "x_label": x_label,
            "linewidth": 3,
            "markersize": 10
        }

        for bt_atten in self.rvr["bt_attenuation"]:
            y_label.insert(0, 'Throughput (Mbps)')
            legends[bt_atten].insert(
                0, str("BT Attenuation @ %sdB" % bt_atten))
            data_sets[bt_atten]["attenuation"] = (
                self.rvr[bt_atten]["attenuation"])
            data_sets[bt_atten]["throughput_received"] = (
                self.rvr[bt_atten]["throughput_received"])
        shaded_region = None

        if "performance_result_path" in self.user_params["test_params"]:
            try:
                attenuation_path = [
                    file_name for file_name in self.performance_files_list
                    if test_name in file_name
                ]
                attenuation_path = attenuation_path[0]
                with open(attenuation_path, 'r') as throughput_file:
                    throughput_results = json.load(throughput_file)
                for bt_atten in self.bt_atten_range:
                    throughput_received = []
                    legends[bt_atten].insert(
                        0, ('Performance Results @ {}dB'.format(bt_atten)))
                    throughput_attenuation = [
                        att +
                        (throughput_results[str(bt_atten)]["fixed_attenuation"])
                        for att in self.rvr[bt_atten]["attenuation"]
                    ]
                    for idx, _ in enumerate(throughput_attenuation):
                        throughput_received.append(throughput_results[str(
                            bt_atten)]["throughput_received"][idx])
                    data_sets[bt_atten][
                        "user_attenuation"] = throughput_attenuation
                    data_sets[bt_atten]["user_throughput"] = throughput_received
                throughput_limits = self.get_throughput_limits(attenuation_path)
                shaded_region = defaultdict(dict)
                for bt_atten in self.bt_atten_range:
                    shaded_region[bt_atten] = {}
                    shaded_region[bt_atten] = {
                        "x_vector": throughput_limits[bt_atten]["attenuation"],
                        "lower_limit":
                        throughput_limits[bt_atten]["lower_limit"],
                        "upper_limit":
                        throughput_limits[bt_atten]["upper_limit"]
                    }
            except Exception as e:
                shaded_region = None
                self.log.warning("ValueError: Performance file not found")

        if self.a2dp_streaming:
            for bt_atten in self.bt_atten_range:
                legends[bt_atten].insert(
                    0, ('Packet drops(in %) @ {}dB'.format(bt_atten)))
                data_sets[bt_atten]["a2dp_attenuation"] = (
                    self.rvr[bt_atten]["attenuation"])
                data_sets[bt_atten]["a2dp_packet_drops"] = (
                    self.rvr[bt_atten]["a2dp_packet_drop"])
            y_label.insert(0, "Packets Dropped")
        fig_property["y_label"] = y_label
        output_file_path = os.path.join(self.pri_ad.log_path, test_name,
                                        "attenuation_plot.html")
        bokeh_chart_plot(
            list(self.rvr["bt_attenuation"]),
            data_sets,
            legends,
            fig_property,
            shaded_region=shaded_region,
            output_file_path=output_file_path)

    def total_attenuation(self, performance_dict):
        """Calculates attenuation with adding fixed attenuation.

        Args:
            performance_dict: dict containing attenuation and fixed attenuation.

        Returns:
            Total attenuation is returned.
        """
        if "fixed_attenuation" in self.test_params:
            total_atten = [
                att + performance_dict["fixed_attenuation"]
                for att in performance_dict["attenuation"]
            ]
            return total_atten

    def throughput_pass_fail_check(self):
        """Check the test result and decide if it passed or failed
        by comparing with throughput limits.The pass/fail tolerances are
        provided in the config file.

        Returns:
            True if successful, False otherwise.
        """
        test_name = self.current_test_name
        try:
            performance_path = [
                file_name for file_name in self.performance_files_list
                if test_name in file_name
            ]
            performance_path = performance_path[0]
            throughput_limits = self.get_throughput_limits(performance_path)

            failure_count = 0
            for bt_atten in self.bt_atten_range:
                for idx, current_throughput in enumerate(
                        self.rvr[bt_atten]["throughput_received"]):
                    current_att = self.rvr[bt_atten]["attenuation"][idx] + (
                        self.rvr[bt_atten]["fixed_attenuation"])
                    if (current_throughput <
                            (throughput_limits[bt_atten]["lower_limit"][idx]) or
                            current_throughput >
                            (throughput_limits[bt_atten]["upper_limit"][idx])):
                        failure_count = failure_count + 1
                        self.log.info(
                            "Throughput at {} dB attenuation is beyond limits. "
                            "Throughput is {} Mbps. Expected within [{}, {}] Mbps.".
                            format(
                                current_att, current_throughput,
                                throughput_limits[bt_atten]["lower_limit"][idx],
                                throughput_limits[bt_atten]["upper_limit"][
                                    idx]))
                if failure_count >= self.test_params["failure_count_tolerance"]:
                    self.log.error(
                        "Test failed. Found {} points outside throughput limits.".
                        format(failure_count))
                    return False
                self.log.info(
                    "Test passed. Found {} points outside throughput limits.".
                    format(failure_count))
        except Exception as e:
            self.log.warning("ValueError: Performance file not found cannot "
                             "calculate throughput limits")

    def get_throughput_limits(self, performance_path):
        """Compute throughput limits for current test.

        Checks the RvR test result and compares to a throughput limits for
        the same configuration. The pass/fail tolerances are provided in the
        config file.

        Args:
            performance_path: path to baseline file used to generate limits

        Returns:
            throughput_limits: dict containing attenuation and throughput
            limit data
        """
        with open(performance_path, 'r') as performance_file:
            performance_results = json.load(performance_file)
        throughput_limits = defaultdict(dict)
        for bt_atten in self.bt_atten_range:
            performance_attenuation = (self.total_attenuation(
                performance_results[str(bt_atten)]))
            attenuation = []
            lower_limit = []
            upper_limit = []
            for idx, current_throughput in enumerate(
                    self.rvr[bt_atten]["throughput_received"]):
                current_att = self.rvr[bt_atten]["attenuation"][idx] + (
                    self.rvr[bt_atten]["fixed_attenuation"])
                att_distances = [
                    abs(current_att - performance_att)
                    for performance_att in performance_attenuation
                ]
                sorted_distances = sorted(
                    enumerate(att_distances), key=lambda x: x[1])
                closest_indeces = [dist[0] for dist in sorted_distances[0:3]]
                closest_throughputs = [
                    performance_results[str(bt_atten)]["throughput_received"][
                        index] for index in closest_indeces
                ]
                closest_throughputs.sort()
                attenuation.append(current_att)
                lower_limit.append(
                    max(closest_throughputs[0] -
                        max(self.test_params["abs_tolerance"],
                            closest_throughputs[0] *
                            self.test_params["pct_tolerance"] / 100), 0))
                upper_limit.append(closest_throughputs[-1] + max(
                    self.test_params["abs_tolerance"], closest_throughputs[-1] *
                    self.test_params["pct_tolerance"] / 100))
            throughput_limits[bt_atten]["attenuation"] = attenuation
            throughput_limits[bt_atten]["lower_limit"] = lower_limit
            throughput_limits[bt_atten]["upper_limit"] = upper_limit
        return throughput_limits
