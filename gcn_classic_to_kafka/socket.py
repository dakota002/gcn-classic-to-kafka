#
# Copyright © 2023 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
"""Protocol handler for GCN socket connection."""

import asyncio
import logging
import struct

import confluent_kafka
import gcn
import lxml.etree

from . import metrics
from .common import notice_type_int_to_str, topic_for_notice_type_str

log = logging.getLogger(__name__)

bin_len = 160
int4 = struct.Struct("!l")
ignore_notice_types = {
    gcn.NoticeType.IM_ALIVE,
    gcn.NoticeType.VOE_11_IM_ALIVE,
    gcn.NoticeType.VOE_20_IM_ALIVE,
}


def client_connected(producer: confluent_kafka.Producer, timeout: float = 90):
    async def client_connected_cb(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        async def read():
            bin_data = await reader.readexactly(bin_len)
            (voe_len,) = int4.unpack(await reader.readexactly(int4.size))
            voe_data = await reader.readexactly(voe_len)
            (txt_len,) = int4.unpack(await reader.readexactly(int4.size))
            txt_data = await reader.readexactly(txt_len)
            log.debug("Read %d + %d + %d bytes", bin_len, voe_len, txt_len)
            return bin_data, voe_data, txt_data

        async def process():
            bin_data, voe_data, txt_data = await asyncio.wait_for(read(), timeout)
            metrics.iamalive.inc()

            (bin_notice_type,) = int4.unpack_from(bin_data)
            log.info("Received notice of type 0x%08X", bin_notice_type)
            if bin_notice_type in ignore_notice_types:
                return

            voe = lxml.etree.fromstring(voe_data)
            voe_notice_type = gcn.handlers.get_notice_type(voe)

            if bin_notice_type != voe_notice_type:
                log.warning(
                    "Binary (0x%08X) and VOEvent (0x%08X) notice types differ",
                    bin_notice_type,
                    voe_notice_type,
                )

            # The text notices do not contain a machine-readable notice type.
            txt_notice_type = bin_notice_type

            for notice_type_int, data, flavor in [
                [bin_notice_type, bin_data, "binary"],
                [voe_notice_type, voe_data, "voevent"],
                [txt_notice_type, txt_data, "text"],
            ]:
                notice_type_str = notice_type_int_to_str(notice_type_int)
                metrics.received.labels(notice_type_int, notice_type_str, flavor).inc()
                topic = topic_for_notice_type_str(notice_type_str, flavor)
                producer.produce(topic, data)

            # Wait for any outstanding messages to be delivered and delivery
            # report callbacks to be triggered.
            producer.poll(0)

        peer, *_ = writer.get_extra_info("peername")
        log.info("Client connected from %s", peer)
        try:
            with metrics.connected.track_inprogress():
                while True:
                    await process()
        finally:
            log.info("Closing connection from %s", peer)
            writer.close()
            await writer.wait_closed()

    return client_connected_cb
