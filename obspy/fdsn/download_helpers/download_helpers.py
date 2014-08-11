#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Download helpers.

:copyright:
    Lion Krischer (krischer@geophysik.uni-muenchen.de), 2014
:license:
    GNU Lesser General Public License, Version 3
    (http://www.gnu.org/copyleft/lesser.html)
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
from future.builtins import *  # NOQA
from future import standard_library
with standard_library.hooks():
    from urllib.error import HTTPError, URLError

import copy
import itertools
import logging
from multiprocessing.pool import ThreadPool
import os
from socket import timeout as SocketTimeout
import shutil
import tempfile
import warnings

import obspy
from obspy.core.util.obspy_types import OrderedDict
from obspy.fdsn.header import URL_MAPPINGS, FDSNException
from obspy.fdsn import Client

from . import utils

ERRORS = (FDSNException, HTTPError, URLError, SocketTimeout)


# Setup the logger.
logger = logging.getLogger("obspy-download-helper")
logger.setLevel(logging.DEBUG)
# Prevent propagating to higher loggers.
logger.propagate = 0
# Console log handler.
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
# Add formatter
FORMAT = "[%(asctime)s] - %(name)s - %(levelname)s: %(message)s"
formatter = logging.Formatter(FORMAT)
ch.setFormatter(formatter)
logger.addHandler(ch)


class FDSNDownloadHelperException(FDSNException):
    pass


class Restrictions(object):
    """
    Class storing non-domain bounds restrictions of a query.
    """
    def __init__(self, starttime, endtime, network=None, station=None,
                 location=None, channel=None,
                 minimum_interstation_distance_in_m=1000,
                 channel_priorities=("HH[Z,N,E]", "BH[Z,N,E]",
                                     "MH[Z,N,E]", "EH[Z,N,E]",
                                     "LH[Z,N,E]"),
                 location_priorities=("", "00", "10")):
        self.starttime = obspy.UTCDateTime(starttime)
        self.endtime = obspy.UTCDateTime(endtime)
        self.network = network
        self.station = station
        self.location = location
        self.channel = channel
        self.channel_priorities = channel_priorities
        self.location_priorities = location_priorities
        self.minimum_interstation_distance_in_m = \
            float(minimum_interstation_distance_in_m)


class DownloadHelper(object):
    def __init__(self, providers=None):
        """
        :param providers: List of FDSN client names or service URLS. Will use
            all FDSN implementations known to ObsPy if set to None. The order
            in the list also determines the priority, if data is available at
            more then one provider it will always be downloaded from the
            provider that comes first in the list.
        """
        if providers is None:
            providers = URL_MAPPINGS.keys()
            # In that case make sure IRIS is first, and ORFEUS second! The
            # remaining items will be sorted alphabetically.
            _p = []
            if "IRIS" in providers:
                _p.append("IRIS")
                providers.remove("IRIS")
            if "ORFEUS" in providers:
                _p.append("ORFEUS")
                providers.remove("ORFEUS")
            _p.extend(sorted(providers))
            providers = _p

        # Immutable tuple.
        self.providers = tuple(providers)

        # Each call to self.download() will create a new temporary directory
        # and save it in this variable.
        self.temp_dir = None

        # Initialize all clients.
        self._initialized_clients = OrderedDict()
        self.__initialize_clients()

    def download(self, domain, restrictions, temp_folder,
                 chunk_size=25, threads_per_client=5, mseed_path=None,
                 stationxml_path=None):

        existing_stations = []

        # Do it sequentially for each client.
        for client_name, client in self._initialized_clients.items():
            try:
                info = utils.get_availability_from_client(
                    client, client_name, restrictions, domain, logger)
            except ERRORS as e:
                msg = "Availability for client '%s': %s" % (
                    client_name,  str(e))
                if "no data available" in msg.lower():
                    logger.info(msg)
                else:
                    logger.error(msg)
                continue

            availability = info["availability"]
            if not availability:
                logger.info("Availability for client '%s': No suitable "
                            "data found." % client_name)
                continue

            # Two cases. Reliable availability means that the client
            # supports the `matchtimeseries` or `includeavailability`
            # parameter and thus the availability information is treated as
            # reliable. Otherwise not. If it is reliable, the stations will
            # be filtered before they are downloaded, otherwise it will
            # happen in reverse to assure as much data as possible is
            # downloaded.
            if info["reliable"]:
                #stations_to_download = [
                #    _i for _i in utils.merge_stations(existing_stations,
                #                                      availability.values())
                #    if _i.client == client_name]
                ### # Now first download the data.
                ### downloaded_miniseed_files = self.download_mseed(
                ###     stations_to_download, mseed_path, temp_folder,
                ###     chunk_size, threads_per_client)
                ### # Now download the station information for the downloaded
                ### # stations.
                downloaded_stations = availability.values()
                downloaded_stations = self.download_stationxml(
                    client, client_name, downloaded_stations,
                    restrictions, threads_per_client)
                # Remove waveforms that did not succeed in having available
                # stationxml files.
                #self.delete_extraneous_waveform_files()
                import ipdb; ipdb.set_trace()
                existing_stations.extend(downloaded_stations)
            else:
                # If it is not reliable, e.g. the client does not have the
                # "includeavailability" or "matchtimeseries" flags,
                # then first download everything and filter later!
                downloaded_miniseed_files = self.download_mseed(
                    availability.values(), restrictions, mseed_path,
                    temp_folder, chunk_size=chunk_size,
                    threads_per_client=threads_per_client)
                # Once that is done, filter as the coordinates of all
                # stations are already known.
                remaining_miniseed_files = self.do_stuff()
                downloaded_stations = self.download_stationxml(
                    remaining_miniseed_files, restrictions,
                    stations_to_download,
                    threads_per_client=threads_per_client)
                self.delete_extraneous_waveform_files()
                existing_stations.extend(downloaded_miniseed_files)


    def download_mseed(self, availability, restrictions, mseed_path,
                       temp_folder, chunk_size=25, threads_per_client=5):
        if not os.path.exists(temp_folder):
            os.makedirs(temp_folder)

        avail = {}
        for c, stations in availability.items():
            stations = copy.deepcopy(stations)
            for station in stations:
                channels = []
                for channel in station.channels:
                    filename = utils.get_mseed_filename(mseed_path,
                                                        station.network,
                                                        station.station,
                                                        channel.location,
                                                        channel.channel)
                    channels.append((channel, filename))
                station.channels[:] = channels
            # Split into chunks.
            avail[c] = [stations[i:i + chunk_size]
                        for i in range(0, len(stations), chunk_size)]

        def star_download_mseed(args):
            try:
                ret_val = utils.download_and_split_mseed_bulk(
                    *args, temp_folder=temp_folder, logger=logger)
            except ERRORS as e:
                msg = ("Client '%s': " % args[1]) + str(e)
                if "no data available" in msg.lower():
                    logger.info(msg)
                else:
                    logger.error(msg)
                return None
            return None

        thread_pools = []
        thread_results = []
        for c, chunks in avail.items():
            client_name, client = c

            p = ThreadPool(min(threads_per_client, len(chunks)))
            thread_pools.append(p)

            thread_results.append(p.map(
                star_download_mseed, [
                    (client, client_name, restrictions.starttime,
                     restrictions.endtime, chunk) for chunk in chunks]))

        for p in thread_pools:
            p.close()

    def download_stationxml(self, client, client_name, station_availability, restrictions,
                            threads_per_client=5):

        avail = []
        for station in station_availability:
            station.stationxml_filename = 'test'
            filename = utils.get_stationxml_filename(station.stationxml_filename,
                                                     station.network,
                                                     station.station)
            avail.append((station, filename))

        def star_download_station(args):
            try:
                utils.download_stationxml(*args, logger=logger)
            except ERRORS as e:
                logger.error(str(e))
                return None
            return None

        thread_pools = []
        thread_results = []
        for s in avail:
            p = ThreadPool(min(threads_per_client, len(s)))
            thread_pools.append(p)

            thread_results.append(p.map(
                star_download_station, [
                    (client, client_name, restrictions.starttime,
                     restrictions.endtime, s[0], s[1])]))

        for p in thread_pools:
            p.close()

        all_channels = []
        for check_xml in avail:
            all_channels.extend(utils.get_stationxml_contents(check_xml[1]))
        import ipdb; ipdb.set_trace()

    def __initialize_clients(self):
        """
        Initialize all clients.
        """
        logger.info("Initializing FDSN clients for %s."
                    % ", ".join(self.providers))

        def _get_client(client_name):
            try:
                this_client = Client(client_name)
            except ERRORS:
                logger.warn("Failed to initialize client '%s'."
                            % client_name)
                return client_name, None
            services = sorted([_i for _i in this_client.services.keys()
                               if not _i.startswith("available")])
            if "dataselect" not in services or "station" not in services:
                logger.info("Cannot use client '%s' as it does not have "
                            "'dataselect' and/or 'station' services."
                            % client_name)
                return client_name, None
            return client_name, this_client

        # Catch warnings in the main thread. The catch_warnings() context
        # manager does not reliably work when used in multiple threads.
        p = ThreadPool(len(self.providers))
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            clients = p.map(_get_client, self.providers)
        p.close()
        for warning in w:
            logger.debug("Warning during initializing one of the clients: " +
                         str(warning.message))

        clients = {key: value for key, value in clients if value is not None}
        # Write to initialized clients dictionary preserving order.
        for client in self.providers:
            if client not in clients:
                continue
            self._initialized_clients[client] = clients[client]

        logger.info("Successfully initialized %i clients: %s."
                    % (len(self._initialized_clients),
                       ", ".join(self._initialized_clients.keys())))
