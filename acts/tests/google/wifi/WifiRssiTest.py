#!/usr/bin/env python3.4
#
#   Copyright 2018 - The Android Open Source Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import collections
import json
import logging
import math
import os
import statistics
from acts import asserts
from acts import base_test
from acts import utils
from acts.controllers import iperf_server as ipf
from acts.metrics.loggers.blackbox import BlackboxMetricLogger
from acts.test_decorators import test_tracker_info
from acts.test_utils.wifi import wifi_performance_test_utils as wputils
from acts.test_utils.wifi import wifi_retail_ap as retail_ap
from acts.test_utils.wifi import wifi_test_utils as wutils
from concurrent.futures import ThreadPoolExecutor

SHORT_SLEEP = 1
MED_SLEEP = 6
CONST_3dB = 3.01029995664
RSSI_ERROR_VAL = float("nan")


class WifiRssiTest(base_test.BaseTestClass):
    """Class to test WiFi RSSI reporting.

    This class tests RSSI reporting on android devices. The class tests RSSI
    accuracy by checking RSSI over a large attenuation range, checks for RSSI
    stability over time when attenuation is fixed, and checks that RSSI quickly
    and reacts to changes attenuation by checking RSSI trajectories over
    configurable attenuation waveforms.For an example config file to run this
    test class see example_connectivity_performance_ap_sta.json.
    """

    def __init__(self, controllers):
        base_test.BaseTestClass.__init__(self, controllers)
        test_metrics = [
            "signal_poll_rssi_shift", "signal_poll_avg_rssi_shift",
            "scan_rssi_shift", "chain_0_rssi_shift", "chain_1_rssi_shift",
            "signal_poll_rssi_error", "signal_poll_avg_rssi_error",
            "scan_rssi_error", "chain_0_rssi_error", "chain_1_rssi_error",
            "signal_poll_rssi_stdev", "chain_0_rssi_stdev",
            "chain_1_rssi_stdev"
        ]
        for metric in test_metrics:
            setattr(
                self,
                "{}_metric".format(metric),
                BlackboxMetricLogger.for_test_case(metric_name=metric))

    def setup_class(self):
        self.dut = self.android_devices[0]
        req_params = [
            "RemoteServer", "RetailAccessPoints", "rssi_test_params",
            "main_network", "testbed_params"
        ]
        self.unpack_userparams(req_params)
        self.test_params = self.rssi_test_params
        self.num_atten = self.attenuators[0].instrument.num_atten
        self.iperf_server = self.iperf_servers[0]
        self.iperf_client = self.iperf_clients[0]
        self.access_point = retail_ap.create(self.RetailAccessPoints)[0]
        self.log_path = os.path.join(logging.log_path, "results")
        utils.create_dir(self.log_path)
        self.log.info("Access Point Configuration: {}".format(
            self.access_point.ap_settings))
        self.testclass_results = []

    def teardown_test(self):
        self.iperf_server.stop()

    def pass_fail_check_rssi_stability(self, postprocessed_results):
        """Check the test result and decide if it passed or failed.

        Checks the RSSI test result and fails the test if the standard
        deviation of signal_poll_rssi is beyond the threshold defined in the
        config file.

        Args:
            postprocessed_results: compiled arrays of RSSI measurements
        """
        # Set Blackbox metric values
        self.signal_poll_rssi_stdev_metric.metric_value = max(
            postprocessed_results["signal_poll_rssi"]["stdev"])
        self.chain_0_rssi_stdev_metric.metric_value = max(
            postprocessed_results["chain_0_rssi"]["stdev"])
        self.chain_1_rssi_stdev_metric.metric_value = max(
            postprocessed_results["chain_1_rssi"]["stdev"])
        # Evaluate test pass/fail
        test_failed = any([
            stdev > self.test_params["stdev_tolerance"]
            for stdev in postprocessed_results["signal_poll_rssi"]["stdev"]
        ])
        test_message = (
            "RSSI stability {0}. Standard deviation was {1} dB "
            "(limit {2}), per chain standard deviation [{3}, {4}] dB".format(
                "failed" * test_failed + "passed" * (not test_failed), [
                    float("{:.2f}".format(x))
                    for x in postprocessed_results["signal_poll_rssi"]["stdev"]
                ], self.test_params["stdev_tolerance"], [
                    float("{:.2f}".format(x))
                    for x in postprocessed_results["chain_0_rssi"]["stdev"]
                ], [
                    float("{:.2f}".format(x))
                    for x in postprocessed_results["chain_1_rssi"]["stdev"]
                ]))
        if test_failed:
            asserts.fail(test_message)
        asserts.explicit_pass(test_message)

    def pass_fail_check_rssi_accuracy(self, postprocessed_results,
                                      rssi_under_test, absolute_accuracy):
        """Check the test result and decide if it passed or failed.

        Checks the RSSI test result and compares and compute its deviation from
        the predicted RSSI. This computation is done for all reported RSSI
        values. The test fails if any of the RSSI values specified in
        rssi_under_test have an average error beyond what is specified in the
        configuration file.

        Args:
            postprocessed_results: compiled arrays of RSSI measurements
            rssi_under_test: list of RSSIs under test, i.e., can cause test to
            fail
            absolute_accuracy: boolean indicating whether to look at absolute
            RSSI accuracy, or centered RSSI accuracy. Centered accuracy is
            computed after systematic RSSI shifts are removed.
        """
        test_failed = False
        test_message = ""
        if absolute_accuracy:
            error_type = "absolute"
        else:
            error_type = "centered"

        for key, val in postprocessed_results.items():
            # Compute the error metrics ignoring invalid RSSI readings
            # If all readings invalid, set error to RSSI_ERROR_VAL
            if "rssi" in key and "predicted" not in key:
                filtered_error = [x for x in val["error"] if not math.isnan(x)]
                if filtered_error:
                    avg_shift = statistics.mean(filtered_error)
                    if absolute_accuracy:
                        avg_error = statistics.mean(
                            [abs(x) for x in filtered_error])
                    else:
                        avg_error = statistics.mean(
                            [abs(x - avg_shift) for x in filtered_error])
                else:
                    avg_error = RSSI_ERROR_VAL
                    avg_shift = RSSI_ERROR_VAL
                # Set Blackbox metric values
                setattr(
                    getattr(self, "{}_error_metric".format(key)),
                    "metric_value", avg_error)
                setattr(
                    getattr(self, "{}_shift_metric".format(key)),
                    "metric_value", avg_shift)
                # Evaluate test pass/fail
                rssi_failure = (avg_error > self.test_params["abs_tolerance"]
                                ) or math.isnan(avg_error)
                if rssi_failure and key in rssi_under_test:
                    test_message = test_message + (
                        "{} failed ({} error = {:.2f} dB, "
                        "shift = {:.2f} dB)\n").format(key, error_type,
                                                       avg_error, avg_shift)
                    test_failed = True
                elif rssi_failure:
                    test_message = test_message + (
                        "{} failed (ignored) ({} error = {:.2f} dB, "
                        "shift = {:.2f} dB)\n").format(key, error_type,
                                                       avg_error, avg_shift)
                else:
                    test_message = test_message + (
                        "{} passed ({} error = {:.2f} dB, "
                        "shift = {:.2f} dB)\n").format(key, error_type,
                                                       avg_error, avg_shift)
        if test_failed:
            asserts.fail(test_message)
        asserts.explicit_pass(test_message)

    def post_process_rssi_sweep(self, rssi_result):
        """Postprocesses and saves JSON formatted results.

        Args:
            rssi_result: dict containing attenuation, rssi and other meta
            data
        Returns:
            postprocessed_results: compiled arrays of RSSI data used in
            pass/fail check
        """
        # Save output as text file
        results_file_path = "{}/{}.json".format(self.log_path,
                                                self.current_test_name)
        with open(results_file_path, 'w') as results_file:
            json.dump(rssi_result, results_file, indent=4)
        # Compile results into arrays of RSSIs suitable for plotting
        # yapf: disable
        postprocessed_results = collections.OrderedDict(
            [("signal_poll_rssi", {}),
             ("signal_poll_avg_rssi", {}),
             ("scan_rssi", {}),
             ("chain_0_rssi", {}),
             ("chain_1_rssi", {}),
             ("total_attenuation", []),
             ("predicted_rssi", [])])
        # yapf: enable
        for key, val in postprocessed_results.items():
            if "scan_rssi" in key:
                postprocessed_results[key]["data"] = [
                    x for data_point in rssi_result["rssi_result"] for x in
                    data_point[key][rssi_result["connected_bssid"]]["data"]
                ]
                postprocessed_results[key]["mean"] = [
                    x[key][rssi_result["connected_bssid"]]["mean"]
                    for x in rssi_result["rssi_result"]
                ]
                postprocessed_results[key]["stdev"] = [
                    x[key][rssi_result["connected_bssid"]]["stdev"]
                    for x in rssi_result["rssi_result"]
                ]
            elif "predicted_rssi" in key:
                postprocessed_results["total_attenuation"] = [
                    att + rssi_result["fixed_attenuation"] +
                    rssi_result["dut_front_end_loss"]
                    for att in rssi_result["attenuation"]
                ]
                postprocessed_results["predicted_rssi"] = [
                    rssi_result["ap_tx_power"] - att
                    for att in postprocessed_results["total_attenuation"]
                ]
            elif "rssi" in key:
                postprocessed_results[key]["data"] = [
                    x for data_point in rssi_result["rssi_result"]
                    for x in data_point[key]["data"]
                ]
                postprocessed_results[key]["mean"] = [
                    x[key]["mean"] for x in rssi_result["rssi_result"]
                ]
                postprocessed_results[key]["stdev"] = [
                    x[key]["stdev"] for x in rssi_result["rssi_result"]
                ]
        # Compute RSSI errors
        for key, val in postprocessed_results.items():
            if "chain" in key:
                postprocessed_results[key]["error"] = [
                    postprocessed_results[key]["mean"][idx] + CONST_3dB -
                    postprocessed_results["predicted_rssi"][idx]
                    for idx in range(
                        len(postprocessed_results["predicted_rssi"]))
                ]
            elif "rssi" in key and "predicted" not in key:
                postprocessed_results[key]["error"] = [
                    postprocessed_results[key]["mean"][idx] -
                    postprocessed_results["predicted_rssi"][idx]
                    for idx in range(
                        len(postprocessed_results["predicted_rssi"]))
                ]
        return postprocessed_results

    def plot_rssi_vs_attenuation(self, postprocessed_results):
        """Function to plot RSSI vs attenuation sweeps

        Args:
            postprocessed_results: compiled arrays of RSSI data.
        """
        data_sets = [[
            postprocessed_results["total_attenuation"],
            postprocessed_results["total_attenuation"],
            postprocessed_results["total_attenuation"],
            postprocessed_results["total_attenuation"],
            postprocessed_results["total_attenuation"],
            postprocessed_results["total_attenuation"]
        ], [
            postprocessed_results["signal_poll_rssi"]["mean"],
            postprocessed_results["signal_poll_avg_rssi"]["mean"],
            postprocessed_results["scan_rssi"]["mean"],
            postprocessed_results["chain_0_rssi"]["mean"],
            postprocessed_results["chain_1_rssi"]["mean"],
            postprocessed_results["predicted_rssi"]
        ]]
        legends = [
            "Signal Poll RSSI", "Signal Poll AVG_RSSI", "Scan RSSI",
            "Chain 0 RSSI", "Chain 1 RSSI", "Predicted RSSI"
        ]
        fig_property = {
            "title": self.current_test_name,
            "x_label": 'Attenuation (dB)',
            "y_label": 'RSSI (dBm)',
            "linewidth": 3,
            "markersize": 10
        }
        output_file_path = "{}/{}.html".format(self.log_path,
                                               self.current_test_name)
        wputils.bokeh_plot(
            data_sets,
            legends,
            fig_property,
            shaded_region=None,
            output_file_path=output_file_path)

    def plot_rssi_vs_time(self, rssi_result, postprocessed_results,
                          center_curves):
        """Function to plot RSSI vs time.

        Args:
            rssi_result: dict containing raw RSSI data
            postprocessed_results: compiled arrays of RSSI data
            center_curvers: boolean indicating whether to shift curves to align
            them with predicted RSSIs
        """
        x_data = []
        y_data = []
        legends = []
        # yapf: disable
        rssi_time_series = collections.OrderedDict(
            [("signal_poll_rssi", []),
             ("signal_poll_avg_rssi", []),
             ("scan_rssi", []),
             ("chain_0_rssi", []),
             ("chain_1_rssi", []),
             ("predicted_rssi", [])])
        # yapf: enable
        for key, val in rssi_time_series.items():
            if "predicted_rssi" in key:
                rssi_time_series[key] = [
                    x for x in postprocessed_results[key] for copies in range(
                        len(rssi_result["rssi_result"][0]["signal_poll_rssi"][
                            "data"]))
                ]
            elif "rssi" in key:
                if center_curves:
                    filtered_error = [
                        x for x in postprocessed_results[key]["error"]
                        if not math.isnan(x)
                    ]
                    if filtered_error:
                        avg_shift = statistics.mean(filtered_error)
                    else:
                        avg_shift = 0
                    rssi_time_series[key] = [
                        x - avg_shift
                        for x in postprocessed_results[key]["data"]
                    ]
                else:
                    rssi_time_series[key] = postprocessed_results[key]["data"]
            time_vec = [
                self.test_params["polling_frequency"] * x
                for x in range(len(rssi_time_series[key]))
            ]
            if len(rssi_time_series[key]) > 0:
                x_data.append(time_vec)
                y_data.append(rssi_time_series[key])
                legends.append(key)
        data_sets = [x_data, y_data]
        fig_property = {
            "title": self.current_test_name,
            "x_label": 'Time (s)',
            "y_label": center_curves * 'Centered' + 'RSSI (dBm)',
            "linewidth": 3,
            "markersize": 0
        }
        output_file_path = "{}/{}.html".format(self.log_path,
                                               self.current_test_name)
        wputils.bokeh_plot(
            data_sets,
            legends,
            fig_property,
            shaded_region=None,
            output_file_path=output_file_path)

    def rssi_test(self, iperf_traffic, connected_measurements,
                  scan_measurements, bssids, polling_frequency,
                  first_measurement_delay):
        """Test function to run RSSI tests.

        The function runs an RSSI test in the current device/AP configuration.
        Function is called from another wrapper function that sets up the
        testbed for the RvR test

        Args:
            iperf_traffic: boolean specifying whether or not to run traffic
            during RSSI tests
            connected_measurements: number of RSSI measurements to make for the
            connected AP per attenuation point
            scan_measurements: number of scans and scan RSSIs to make per
            attenuation point
            bssids: list of BSSIDs to monitor in scans
            polling_frequency: time between connected AP measurements
        Returns:
            rssi_result: dict containing rssi_result and meta data
        """
        self.log.info("Start running RSSI test.")
        rssi_result = []
        # Start iperf traffic if required by test
        if self.iperf_traffic:
            self.iperf_server.start(tag=0)
            if isinstance(self.iperf_server, ipf.IPerfServerOverAdb):
                iperf_server_address = self.dut_ip
            else:
                iperf_server_address = self.testbed_params[
                    "iperf_server_address"]
            executor = ThreadPoolExecutor(max_workers=1)
            thread_future = executor.submit(
                self.iperf_client.start, iperf_server_address, self.iperf_args,
                0, self.iperf_timeout + SHORT_SLEEP)
            executor.shutdown(wait=False)
        for atten in self.rssi_atten_range:
            # Set Attenuation
            self.log.info("Setting attenuation to {} dB".format(atten))
            for attenuator in self.attenuators:
                attenuator.set_atten(atten)
            current_rssi = collections.OrderedDict()
            current_rssi = wputils.get_connected_rssi(
                self.dut, connected_measurements, polling_frequency,
                first_measurement_delay)
            current_rssi["scan_rssi"] = wputils.get_scan_rssi(
                self.dut, bssids, scan_measurements)
            rssi_result.append(current_rssi)
            self.log.info("Connected RSSI at {0:.2f} dB is {1:.2f} dB".format(
                atten, current_rssi["signal_poll_rssi"]["mean"]))
        # Stop iperf traffic if needed
        for attenuator in self.attenuators:
            attenuator.set_atten(0)
        if self.iperf_traffic:
            thread_future.result()
            self.iperf_server.stop()
        return rssi_result

    def setup_ap(self):
        """Function that gets devices ready for the test."""
        band = self.access_point.band_lookup_by_channel(self.channel)
        if "2G" in band:
            frequency = wutils.WifiEnums.channel_2G_to_freq[self.channel]
        else:
            frequency = wutils.WifiEnums.channel_5G_to_freq[self.channel]
        if frequency in wutils.WifiEnums.DFS_5G_FREQUENCIES:
            self.access_point.set_region(self.testbed_params["DFS_region"])
        else:
            self.access_point.set_region(self.testbed_params["default_region"])
        self.access_point.set_channel(band, self.channel)
        self.access_point.set_bandwidth(band, self.mode)
        self.log.info("Access Point Configuration: {}".format(
            self.access_point.ap_settings))

    def setup_dut(self):
        """Sets up the DUT in the configuration required by the test."""
        band = self.access_point.band_lookup_by_channel(self.channel)
        wutils.wifi_toggle_state(self.dut, True)
        wutils.reset_wifi(self.dut)
        self.main_network[band]["channel"] = self.channel
        self.dut.droid.wifiSetCountryCode(self.test_params["country_code"])
        wutils.wifi_connect(self.dut, self.main_network[band], num_of_tries=5)
        self.dut_ip = self.dut.droid.connectivityGetIPv4Addresses('wlan0')[0]

    def rssi_test_func(self, iperf_traffic, connected_measurements,
                       scan_measurements, bssids, polling_frequency,
                       first_measurement_delay):
        """Main function to test RSSI.

        The function sets up the AP in the correct channel and mode
        configuration and called rssi_test to sweep attenuation and measure
        RSSI

        Returns:
            rssi_result: dict containing rssi_results and meta data
        """
        #Initialize test settings
        rssi_result = collections.OrderedDict()
        # Configure AP
        self.setup_ap()
        # Initialize attenuators
        for attenuator in self.attenuators:
            attenuator.set_atten(self.rssi_atten_range[0])
        # Connect DUT to Network
        self.setup_dut()
        # Run test and log result
        band = self.access_point.band_lookup_by_channel(self.channel)
        rssi_result["test_name"] = self.current_test_name
        rssi_result["ap_settings"] = self.access_point.ap_settings.copy()
        rssi_result["attenuation"] = list(self.rssi_atten_range)
        rssi_result["connected_bssid"] = self.main_network[band]["BSSID"]
        if "{}_{}".format(str(self.channel),
                          self.mode) in self.testbed_params["ap_tx_power"]:
            rssi_result["ap_tx_power"] = self.testbed_params["ap_tx_power"][
                "{}_{}".format(str(self.channel), self.mode)]
        else:
            rssi_result["ap_tx_power"] = self.testbed_params["ap_tx_power"][
                str(self.channel)]
        rssi_result["fixed_attenuation"] = self.testbed_params[
            "fixed_attenuation"][str(self.channel)]
        rssi_result["dut_front_end_loss"] = self.testbed_params[
            "dut_front_end_loss"][str(self.channel)]
        rssi_result["rssi_result"] = self.rssi_test(
            iperf_traffic, connected_measurements, scan_measurements, bssids,
            polling_frequency, first_measurement_delay)
        self.testclass_results.append(rssi_result)
        return rssi_result

    def get_iperf_timeout(self, atten_range, connected_measurements,
                          polling_frequency, first_measurement_delay,
                          scan_measurements):
        """Function to comput iperf session length required in RSSI test.

        Args:
            atten_range: array of attenuations
            connected_measurements: number of measurements per atten step
            polling_frequency: interval between RSSI measurements
            first_measurement_delay: delay before first measurements
            scan_measurements: number of scan RSSI measurements per atten step
        Returns:
            iperf_timeout: length of iperf session required in rssi test
        """
        atten_step_duration = first_measurement_delay + (
            connected_measurements *
            polling_frequency) + scan_measurements * MED_SLEEP
        iperf_timeout = len(atten_range) * atten_step_duration + MED_SLEEP
        self.log.info("iperf timeout is {}".format(iperf_timeout))
        return iperf_timeout

    def _test_rssi_vs_atten(self):
        """ Function that gets called for each test case of rssi_vs_atten

        The function gets called in each rssi test case. The function
        customizes the test based on the test name of the test that called it
        """
        test_params = self.current_test_name.split("_")
        self.channel = int(test_params[4][2:])
        self.mode = test_params[5]
        self.iperf_traffic = "ActiveTraffic" in test_params[6]
        band = self.access_point.band_lookup_by_channel(self.channel)
        num_atten_steps = int((self.test_params["rssi_vs_atten_stop"] -
                               self.test_params["rssi_vs_atten_start"]) /
                              self.test_params["rssi_vs_atten_step"])
        self.rssi_atten_range = [
            self.test_params["rssi_vs_atten_start"] +
            x * self.test_params["rssi_vs_atten_step"]
            for x in range(0, num_atten_steps)
        ]
        self.iperf_timeout = self.get_iperf_timeout(
            self.rssi_atten_range,
            self.test_params["rssi_vs_atten_connected_measurements"],
            self.test_params["polling_frequency"], MED_SLEEP,
            self.test_params["rssi_vs_atten_scan_measurements"])
        if isinstance(self.iperf_server, ipf.IPerfServerOverAdb):
            self.iperf_args = '-i 1 -t {} -J'.format(self.iperf_timeout)
        else:
            self.iperf_args = '-i 1 -t {} -J -R'.format(self.iperf_timeout)
        rssi_result = self.rssi_test_func(
            self.iperf_traffic,
            self.test_params["rssi_vs_atten_connected_measurements"],
            self.test_params["rssi_vs_atten_scan_measurements"],
            [self.main_network[band]["BSSID"]],
            self.test_params["polling_frequency"], MED_SLEEP)
        postprocessed_results = self.post_process_rssi_sweep(rssi_result)
        self.plot_rssi_vs_attenuation(postprocessed_results)
        self.pass_fail_check_rssi_accuracy(
            postprocessed_results, self.test_params["rssi_vs_atten_metrics"],
            1)

    def _test_rssi_stability(self):
        """ Function that gets called for each test case of rssi_stability

        The function gets called in each stability test case. The function
        customizes test based on the test name of the test that called it
        """
        test_params = self.current_test_name.split("_")
        self.channel = int(test_params[3][2:])
        self.mode = test_params[4]
        self.iperf_traffic = "ActiveTraffic" in test_params[5]
        band = self.access_point.band_lookup_by_channel(self.channel)
        self.rssi_atten_range = self.test_params["rssi_stability_atten"]
        connected_measurements = int(
            self.test_params["rssi_stability_duration"] /
            self.test_params["polling_frequency"])
        self.iperf_timeout = self.get_iperf_timeout(
            self.rssi_atten_range, connected_measurements,
            self.test_params["polling_frequency"], MED_SLEEP, 0)
        if isinstance(self.iperf_server, ipf.IPerfServerOverAdb):
            self.iperf_args = '-i 1 -t {} -J'.format(self.iperf_timeout)
        else:
            self.iperf_args = '-i 1 -t {} -J -R'.format(self.iperf_timeout)
        rssi_result = self.rssi_test_func(
            self.iperf_traffic, connected_measurements, 0,
            [self.main_network[band]["BSSID"]],
            self.test_params["polling_frequency"], MED_SLEEP)
        postprocessed_results = self.post_process_rssi_sweep(rssi_result)
        self.plot_rssi_vs_time(rssi_result, postprocessed_results, 1)
        self.pass_fail_check_rssi_stability(postprocessed_results)

    def _test_rssi_tracking(self):
        """ Function that gets called for each test case of rssi_tracking

        The function gets called in each rssi test case. The function
        customizes the test based on the test name of the test that called it
        """
        test_params = self.current_test_name.split("_")
        self.channel = int(test_params[3][2:])
        self.mode = test_params[4]
        self.iperf_traffic = "ActiveTraffic" in test_params[5]
        band = self.access_point.band_lookup_by_channel(self.channel)
        self.rssi_atten_range = []
        for waveform in self.test_params["rssi_tracking_waveforms"]:
            waveform_vector = []
            for section in range(len(waveform["atten_levels"]) - 1):
                section_limits = waveform["atten_levels"][section:section + 2]
                up_down = (1 - 2 * (section_limits[1] < section_limits[0]))
                temp_section = list(
                    range(section_limits[0], section_limits[1] + up_down,
                          up_down * waveform["step_size"]))
                temp_section = [
                    temp_section[idx] for idx in range(len(temp_section))
                    for n in range(waveform["step_duration"])
                ]
                waveform_vector += temp_section
            waveform_vector = waveform_vector * waveform["repetitions"]
            self.rssi_atten_range = self.rssi_atten_range + waveform_vector
        connected_measurements = int(1 / self.test_params["polling_frequency"])
        self.iperf_timeout = self.get_iperf_timeout(
            self.rssi_atten_range, connected_measurements,
            self.test_params["polling_frequency"], 0, 0)
        if isinstance(self.iperf_server, ipf.IPerfServerOverAdb):
            self.iperf_args = '-i 1 -t {} -J'.format(self.iperf_timeout)
        else:
            self.iperf_args = '-i 1 -t {} -J -R'.format(self.iperf_timeout)
        rssi_result = self.rssi_test_func(
            self.iperf_traffic, connected_measurements, 0,
            [self.main_network[band]["BSSID"]],
            self.test_params["polling_frequency"], 0)
        postprocessed_results = self.post_process_rssi_sweep(rssi_result)
        self.plot_rssi_vs_time(rssi_result, postprocessed_results, 1)
        self.pass_fail_check_rssi_accuracy(postprocessed_results,
                                           ["signal_poll_rssi"], 0)

    @test_tracker_info(uuid='519689b8-0a3c-4fd9-9227-fd7962d0f1a0')
    def test_rssi_stability_ch1_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='23eca2ab-d0b4-4730-9f32-ec2d901ae493')
    def test_rssi_stability_ch2_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='63d340c0-dcf9-4e14-87bd-a068a59836b2')
    def test_rssi_stability_ch3_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='ddbe88d8-be20-40eb-8f29-55049e3fef28')
    def test_rssi_stability_ch4_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='9c06304e-2b60-4619-8fb3-73fd2cb4b854')
    def test_rssi_stability_ch5_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='74b656ca-132e-4d66-9584-560287081607')
    def test_rssi_stability_ch6_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='23b5f19a-539b-4908-a197-06ce505d3d23')
    def test_rssi_stability_ch7_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='e7b85167-f4c4-4adb-a111-04d8a5f10e1a')
    def test_rssi_stability_ch8_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='2a0a9393-4b68-4c08-8787-3f35d1a8458b')
    def test_rssi_stability_ch9_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='069c7acf-3e7e-4298-91cb-d292c6025ae1')
    def test_rssi_stability_ch10_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='95c5a27c-1dea-47a4-a1c5-edf955545f12')
    def test_rssi_stability_ch11_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='8aeab023-a096-4fbe-80dd-fd01466f9fac')
    def test_rssi_stability_ch36_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='872fed9f-d0bb-4a7b-a2a7-bf8df7740b2d')
    def test_rssi_stability_ch36_VHT40_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='27395fd1-e286-473a-b98e-5a50db2a598a')
    def test_rssi_stability_ch36_VHT80_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='6f6b25e3-1a1e-4a61-930a-1d0aa25ba900')
    def test_rssi_stability_ch40_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='c6717da7-855c-4c6e-a6e2-ee42b8feaaab')
    def test_rssi_stability_ch44_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='2e34f735-079c-4619-9e74-b96dc8d0597f')
    def test_rssi_stability_ch44_VHT40_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='d543c019-1ff5-41d4-9b37-ccdc593f3edd')
    def test_rssi_stability_ch48_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='2bb08914-36b2-4f58-9b3e-c3f3f4fac8ab')
    def test_rssi_stability_ch149_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='e2f585f5-7811-4570-b987-23da301eb75d')
    def test_rssi_stability_ch149_VHT40_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='f3e74d5b-73f6-4723-abf3-c9c147db08e3')
    def test_rssi_stability_ch149_VHT80_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='06503ed0-baf3-4cd1-ac5e-4124e3c7f52f')
    def test_rssi_stability_ch153_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='0cf8286f-a919-4e29-a9f2-e7738a4afe8f')
    def test_rssi_stability_ch157_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='f9a0165c-468b-4096-8f4b-cc80bae564a0')
    def test_rssi_stability_ch157_VHT40_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='4b74dd46-4190-4556-8ad8-c55808e9e847')
    def test_rssi_stability_ch161_VHT20_ActiveTraffic(self):
        self._test_rssi_stability()

    @test_tracker_info(uuid='ae54b7cc-d76d-4460-8dcc-2c439265c7c9')
    def test_rssi_vs_atten_ch1_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='07fe7899-886d-45ba-9c1d-7daaf9844c9c')
    def test_rssi_vs_atten_ch2_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='9e86578b-a6cd-4de9-a79d-eabac5bd5f4e')
    def test_rssi_vs_atten_ch3_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='e9d258ca-8e70-408e-b704-782fce7a07c5')
    def test_rssi_vs_atten_ch4_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='1c5d71a0-7532-49e4-98a9-1c2d9d8d58d2')
    def test_rssi_vs_atten_ch5_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='107f01f3-b6b9-470b-9895-6345edfc9599')
    def test_rssi_vs_atten_ch6_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='88cb18b2-30bf-4c01-ac28-15451289e7cd')
    def test_rssi_vs_atten_ch7_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='c07a7442-bd1d-40c7-80ed-167e30b8cfaf')
    def test_rssi_vs_atten_ch8_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='b8946280-88d5-400d-a417-2bdc9d7e054a')
    def test_rssi_vs_atten_ch9_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='a05db91b-740d-4984-a447-79ab438034f0')
    def test_rssi_vs_atten_ch10_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='f4d565f8-f060-462c-9b3c-cd1f7d27b3ea')
    def test_rssi_vs_atten_ch11_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='a33a93ac-604a-414f-ae96-42dffbe59a93')
    def test_rssi_vs_atten_ch36_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='39875ab0-e0e9-464b-8a47-4dedd65f066e')
    def test_rssi_vs_atten_ch36_VHT40_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='c6ff8768-f124-4190-baf2-bbf14b612de3')
    def test_rssi_vs_atten_ch36_VHT80_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='ed4705af-e202-4737-b410-8bab0515e79f')
    def test_rssi_vs_atten_ch40_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='1388df99-ecbf-4412-9ded-d66552f37ec5')
    def test_rssi_vs_atten_ch44_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='06868677-ad3c-4f50-9b9e-ae8d9455ae4d')
    def test_rssi_vs_atten_ch44_VHT40_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='9b6676de-c736-4603-a9b3-97670bea8f25')
    def test_rssi_vs_atten_ch48_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='2641c4b8-0092-4e29-9139-fdb3b3f04d05')
    def test_rssi_vs_atten_ch149_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='c8bc3f7d-b459-4e40-9c73-b0bf534c6c08')
    def test_rssi_vs_atten_ch149_VHT40_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='3e08f5b6-9f3c-4905-8b10-82e1ca830cc9')
    def test_rssi_vs_atten_ch149_VHT80_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='2343efe3-fdda-4180-add7-4786d35e29bb')
    def test_rssi_vs_atten_ch153_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='89a16974-2399-4356-b720-17b765ff1c3a')
    def test_rssi_vs_atten_ch157_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='c8e0e44a-b962-4e71-ba8f-068f268c8823')
    def test_rssi_vs_atten_ch157_VHT40_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    @test_tracker_info(uuid='581b5794-239e-4d1c-b0ce-7c6dc5bd373f')
    def test_rssi_vs_atten_ch161_VHT20_ActiveTraffic(self):
        self._test_rssi_vs_atten()

    def test_rssi_tracking_ch6_VHT20_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch6_VHT20_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT20_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT20_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT40_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT40_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT80_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch36_VHT80_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT20_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT20_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT40_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT40_NoTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT80_ActiveTraffic(self):
        self._test_rssi_tracking()

    def test_rssi_tracking_ch149_VHT80_NoTraffic(self):
        self._test_rssi_tracking()


class WifiRssi_2GHz_ActiveTraffic_Test(WifiRssiTest):
    def __init__(self, controllers):
        super().__init__(controllers)
        self.tests = ("test_rssi_stability_ch1_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch1_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch2_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch2_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch6_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch6_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch10_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch10_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch11_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch11_VHT20_ActiveTraffic")


class WifiRssi_5GHz_ActiveTraffic_Test(WifiRssiTest):
    def __init__(self, controllers):
        super().__init__(controllers)
        self.tests = ("test_rssi_stability_ch36_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch36_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch36_VHT40_ActiveTraffic",
                      "test_rssi_vs_atten_ch36_VHT40_ActiveTraffic",
                      "test_rssi_stability_ch36_VHT80_ActiveTraffic",
                      "test_rssi_vs_atten_ch36_VHT80_ActiveTraffic",
                      "test_rssi_stability_ch40_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch40_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch44_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch44_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch44_VHT40_ActiveTraffic",
                      "test_rssi_vs_atten_ch44_VHT40_ActiveTraffic",
                      "test_rssi_stability_ch48_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch48_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch149_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch149_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch149_VHT40_ActiveTraffic",
                      "test_rssi_vs_atten_ch149_VHT40_ActiveTraffic",
                      "test_rssi_stability_ch149_VHT80_ActiveTraffic",
                      "test_rssi_vs_atten_ch149_VHT80_ActiveTraffic",
                      "test_rssi_stability_ch153_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch153_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch157_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch157_VHT20_ActiveTraffic",
                      "test_rssi_stability_ch157_VHT40_ActiveTraffic",
                      "test_rssi_vs_atten_ch157_VHT40_ActiveTraffic",
                      "test_rssi_stability_ch161_VHT20_ActiveTraffic",
                      "test_rssi_vs_atten_ch161_VHT20_ActiveTraffic")


class WifiRssiTrackingTest(WifiRssiTest):
    def __init__(self, controllers):
        super().__init__(controllers)
        self.tests = ("test_rssi_tracking_ch6_VHT20_ActiveTraffic",
                      "test_rssi_tracking_ch6_VHT20_NoTraffic",
                      "test_rssi_tracking_ch36_VHT20_ActiveTraffic",
                      "test_rssi_tracking_ch36_VHT20_NoTraffic",
                      "test_rssi_tracking_ch36_VHT40_ActiveTraffic",
                      "test_rssi_tracking_ch36_VHT40_NoTraffic",
                      "test_rssi_tracking_ch36_VHT80_ActiveTraffic",
                      "test_rssi_tracking_ch36_VHT80_NoTraffic",
                      "test_rssi_tracking_ch149_VHT20_ActiveTraffic",
                      "test_rssi_tracking_ch149_VHT20_NoTraffic",
                      "test_rssi_tracking_ch149_VHT40_ActiveTraffic",
                      "test_rssi_tracking_ch149_VHT40_NoTraffic",
                      "test_rssi_tracking_ch149_VHT80_ActiveTraffic",
                      "test_rssi_tracking_ch149_VHT80_NoTraffic")
