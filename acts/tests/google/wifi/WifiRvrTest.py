#!/usr/bin/env python3.4
#
#   Copyright 2017 - The Android Open Source Project
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

import json
import logging
import math
import os
import time
from acts import asserts
from acts import base_test
from acts import utils
from acts.controllers import iperf_server as ipf
from acts.test_utils.wifi import wifi_power_test_utils as wputils
from acts.test_utils.wifi import wifi_retail_ap as retail_ap
from acts.test_utils.wifi import wifi_test_utils as wutils

TEST_TIMEOUT = 10
EPSILON = 1e-6


class WifiRvrTest(base_test.BaseTestClass):
    def __init__(self, controllers):
        base_test.BaseTestClass.__init__(self, controllers)

    def setup_class(self):
        self.dut = self.android_devices[0]
        req_params = ["test_params", "main_network"]
        opt_params = ["RetailAccessPoints"]
        self.unpack_userparams(req_params, opt_params)
        self.num_atten = self.attenuators[0].instrument.num_atten
        self.iperf_server = self.iperf_servers[0]
        self.access_points = retail_ap.create(self.RetailAccessPoints)
        self.access_point = self.access_points[0]
        self.log_path = os.path.join(logging.log_path, "rvr_results")
        utils.create_dir(self.log_path)
        self.log.info("Access Point Configuration: {}".format(
            self.access_point.ap_settings))
        self.testclass_results = []

    def teardown_test(self):
        self.iperf_server.stop()

    def teardown_class(self):
        """Saves plot with all test results to enable comparison.
        """
        # Plot and save all results
        x_data = []
        y_data = []
        legends = []
        for result in self.testclass_results:
            total_attenuation = [
                att + result["fixed_attenuation"]
                for att in result["attenuation"]
            ]
            x_data.append(total_attenuation)
            y_data.append(result["throughput_receive"])
            legends.append(result["test_name"])
        x_label = 'Attenuation (dB)'
        y_label = 'Throughput (Mbps)'
        data_sets = [x_data, y_data]
        fig_property = {
            "title": "RvR Results",
            "x_label": x_label,
            "y_label": y_label,
            "linewidth": 3,
            "markersize": 10
        }
        output_file_path = "{}/{}.html".format(self.log_path, "rvr_results")
        wputils.bokeh_plot(data_sets, legends, fig_property, output_file_path)

    def pass_fail_check(self, rvr_result):
        """Check the test result and decide if it passed or failed.

        Checks the RvR test result and compares to a golden file of results for
        the same configuration. The pass/fail tolerances are provided in the
        config file. Currently, the test fails if any a single point is out of
        range of the corresponding area in the golden file.

        Args:
            rvr_result: dict containing attenuation, throughput and other meta
            data
        """
        test_name = self.current_test_name
        gldn_path = os.path.join(self.test_params["golden_results_path"],
                                 "{}.json".format(test_name))
        with open(gldn_path, 'r') as gldn_file:
            gldn_results = json.load(gldn_file)
            gldn_attenuation = [
                att + gldn_results["fixed_attenuation"]
                for att in gldn_results["attenuation"]
            ]
        for idx, current_throughput in enumerate(
                rvr_result["throughput_receive"]):
            current_att = rvr_result["attenuation"][idx] + rvr_result["fixed_attenuation"]
            att_distances = [
                abs(current_att - gldn_att) for gldn_att in gldn_attenuation
            ]
            sorted_distances = sorted(
                enumerate(att_distances), key=lambda x: x[1])
            closest_indeces = [dist[0] for dist in sorted_distances[0:2]]
            closest_throughputs = [
                gldn_results["throughput_receive"][index]
                for index in closest_indeces
            ]
            closest_throughputs.sort()

            allowed_throughput_range = [
                max(closest_throughputs[0] - max(
                    self.test_params["abs_tolerance"], closest_throughputs[0] *
                    self.test_params["pct_tolerance"] / 100),
                    0), closest_throughputs[1] +
                max(self.test_params["abs_tolerance"], closest_throughputs[1] *
                    self.test_params["pct_tolerance"] / 100)
            ]
            throughput_distance = [
                current_throughput - throughput_limit
                for throughput_limit in allowed_throughput_range
            ]
            if (throughput_distance[0] < -self.test_params["abs_tolerance"]
                    or throughput_distance[1] >
                    self.test_params["abs_tolerance"]):
                asserts.fail(
                    "Throughput at {}dB attenuation is beyond limits. "
                    "Throughput is {} Mbps. Expected within {} Mbps.".format(
                        current_att, current_throughput,
                        allowed_throughput_range))
        asserts.explicit_pass("Measurement finished for %s." % test_name)

    def post_process_results(self, rvr_result):
        """Saves plots and JSON formatted results.

        Args:
            rvr_result: dict containing attenuation, throughput and other meta
            data
        """
        # Save output as text file
        test_name = self.current_test_name
        results_file_path = "{}/{}.json".format(self.log_path,
                                                self.current_test_name)
        with open(results_file_path, 'w') as results_file:
            json.dump(rvr_result, results_file)
        # Plot and save
        legends = [self.current_test_name]
        x_label = 'Attenuation (dB)'
        y_label = 'Throughput (Mbps)'
        total_attenuation = [
            att + rvr_result["fixed_attenuation"]
            for att in rvr_result["attenuation"]
        ]
        data_sets = [[total_attenuation], [rvr_result["throughput_receive"]]]
        fig_property = {
            "title": test_name,
            "x_label": x_label,
            "y_label": y_label,
            "linewidth": 3,
            "markersize": 10
        }
        try:
            gldn_path = os.path.join(self.test_params["golden_results_path"],
                                     "{}.json".format(test_name))
            with open(gldn_path, 'r') as gldn_file:
                gldn_results = json.load(gldn_file)
            legends.insert(0, "Golden Results")
            gldn_attenuation = [
                att + gldn_results["fixed_attenuation"]
                for att in gldn_results["attenuation"]
            ]
            data_sets[0].insert(0, gldn_attenuation)
            data_sets[1].insert(0, gldn_results["throughput_receive"])
        except:
            self.log.warning("ValueError: Golden file not found")
        output_file_path = "{}/{}.html".format(self.log_path, test_name)
        wputils.bokeh_plot(data_sets, legends, fig_property, output_file_path)

    def rvr_test(self):
        """Test function to run RvR.

        The function runs an RvR test in the current device/AP configuration.
        Function is called from another wrapper function that sets up the
        testbed for the RvR test

        Returns:
            rvr_result: dict containing rvr_results and meta data
        """
        self.log.info("Start running RvR")
        rvr_result = []
        for atten in self.rvr_atten_range:
            # Set Attenuation
            self.log.info("Setting attenuation to {} dB".format(atten))
            [
                self.attenuators[i].set_atten(atten)
                for i in range(self.num_atten)
            ]
            # Start iperf session
            self.iperf_server.start(tag=str(atten))
            try:
                client_output = ""
                client_status, client_output = self.dut.run_iperf_client(
                    self.test_params["iperf_server_address"],
                    self.iperf_args,
                    timeout=self.test_params["iperf_duration"] + TEST_TIMEOUT)
            except:
                self.log.warning("TimeoutError: Iperf measurement timed out.")
            client_output_path = os.path.join(
                self.iperf_server.log_path, "iperf_client_output_{}_{}".format(
                    self.current_test_name, str(atten)))
            with open(client_output_path, 'w') as out_file:
                out_file.write("\n".join(client_output))
            self.iperf_server.stop()
            # Parse and log result
            if self.use_client_output:
                iperf_file = client_output_path
            else:
                iperf_file = self.iperf_server.log_files[-1]
            try:
                iperf_result = ipf.IPerfResult(iperf_file)
                curr_throughput = (math.fsum(iperf_result.instantaneous_rates[
                    self.test_params["iperf_ignored_interval"]:-1]) / len(
                        iperf_result.instantaneous_rates[self.test_params[
                            "iperf_ignored_interval"]:-1])) * 8
            except:
                self.log.warning(
                    "ValueError: Cannot get iperf result. Setting to 0")
                curr_throughput = 0
            rvr_result.append(curr_throughput)
            self.log.info("Throughput at {0:d} dB is {1:.2f} Mbps".format(
                atten, curr_throughput))
        [self.attenuators[i].set_atten(0) for i in range(self.num_atten)]
        return rvr_result

    def rvr_test_func(self, channel, mode):
        """Main function to test RvR.

        The function sets up the AP in the correct channel and mode
        configuration and called run_rvr to sweep attenuation and measure
        throughput

        Args:
            channel: Specifies AP's channel
            mode: Specifies AP's bandwidth/mode (11g, VHT20, VHT40, VHT80)
        Returns:
            rvr_result: dict containing rvr_results and meta data
        """
        #Initialize RvR test parameters
        self.rvr_atten_range = range(self.test_params["rvr_atten_start"],
                                     self.test_params["rvr_atten_stop"],
                                     self.test_params["rvr_atten_step"])
        rvr_result = {}
        # Configure AP
        band = self.access_point.band_lookup_by_channel(channel)
        self.access_point.set_channel(band, channel)
        self.access_point.set_bandwidth(band, mode)
        self.log.info("Access Point Configuration: {}".format(
            self.access_point.ap_settings))
        # Set attenuator to 0 dB
        [self.attenuators[i].set_atten(0) for i in range(self.num_atten)]
        # Connect DUT to Network
        wutils.reset_wifi(self.dut)
        self.main_network[band]["channel"] = channel
        wutils.wifi_connect(self.dut, self.main_network[band], num_of_tries=5)
        time.sleep(5)
        # Run RvR and log result
        rvr_result["test_name"] = self.current_test_name
        rvr_result["ap_settings"] = self.access_point.ap_settings.copy()
        rvr_result["attenuation"] = list(self.rvr_atten_range)
        rvr_result["fixed_attenuation"] = self.test_params[
            "fixed_attenuation"][str(channel)]
        rvr_result["throughput_receive"] = self.rvr_test()
        self.testclass_results.append(rvr_result)
        return rvr_result

    def _test_rvr(self):
        """ Function that gets called for each test case

        The function gets called in each rvr test case. The function customizes
        the rvr test based on the test name of the test that called it
        """
        test_params = self.current_test_name.split("_")
        channel = int(test_params[4][2:])
        mode = test_params[5]
        self.iperf_args = '-i 1 -t {} -J '.format(
            self.test_params["iperf_duration"])
        if test_params[2] == "UDP":
            self.iperf_args = self.iperf_args + "-u -b {}".format(
                self.test_params["UDP_rates"][mode])
        if test_params[3] == "DL":
            self.iperf_args = self.iperf_args + ' -R'
            self.use_client_output = True
        else:
            self.use_client_output = False
        rvr_result = self.rvr_test_func(channel, mode)
        self.post_process_results(rvr_result)
        self.pass_fail_check(rvr_result)

    #Test cases
    def test_rvr_TCP_DL_ch1_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch1_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch6_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch6_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch11_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch11_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch36_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch36_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch36_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch36_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch36_VHT80(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch36_VHT80(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch40_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch40_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch44_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch44_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch44_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch44_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch48_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch48_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch149_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch149_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch149_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch149_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch149_VHT80(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch149_VHT80(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch153_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch153_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch157_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch157_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch157_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch157_VHT40(self):
        self._test_rvr()

    def test_rvr_TCP_DL_ch161_VHT20(self):
        self._test_rvr()

    def test_rvr_TCP_UL_ch161_VHT20(self):
        self._test_rvr()

    # UDP Tests
    def test_rvr_UDP_DL_ch161_VHT20(self):
        self._test_rvr()

    def test_rvr_UDP_UL_ch161_VHT20(self):
        self._test_rvr()