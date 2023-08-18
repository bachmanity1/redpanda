/*
 * Copyright 2021 Redpanda Data, Inc.
 *
 * Licensed as a Redpanda Enterprise file under the Redpanda Community
 * License (the "License"); you may not use this file except in compliance with
 * the License. You may obtain a copy of the License at
 *
 * https://github.com/redpanda-data/redpanda/blob/master/licenses/rcl.md
 */

#include "archival/ntp_archiver_service.h"
#include "archival/tests/service_fixture.h"
#include "cluster/commands.h"
#include "net/unresolved_address.h"
#include "storage/types.h"
#include "test_utils/async.h"

#include <seastar/core/deleter.hh>
#include <seastar/core/future-util.hh>
#include <seastar/core/lowres_clock.hh>
#include <seastar/core/sstring.hh>
#include <seastar/testing/thread_test_case.hh>
#include <seastar/util/log.hh>

#include <boost/algorithm/string.hpp>

#include <type_traits>

inline seastar::logger arch_svc_log("SVC-TEST");
static const model::ns test_ns = model::ns("kafka");
using namespace std::chrono_literals;

FIXTURE_TEST(test_reconciliation_manifest_download, archiver_fixture) {
    wait_for_controller_leadership().get();
    auto topic1 = model::topic("topic_1");
    auto topic2 = model::topic("topic_2");
    auto pid0 = model::ntp(test_ns, topic1, model::partition_id(0));
    auto pid1 = model::ntp(test_ns, topic2, model::partition_id(0));
    std::array<const char*, 3> urls = {
      "/10000000/meta/test-namespace/topic_1/0_2/manifest.json",
      "/60000000/meta/test-namespace/topic_2/0_4/manifest.json",
      "/20000000/meta/test-namespace/topic_2/topic_manifest.json",
    };
    ss::sstring manifest_json = R"json({
        "version": 1,
        "namespace": "test-namespace",
        "topic": "test_1",
        "partition": 0,
        "revision": 1,
        "last_offset": 2,
        "segments": {
            "1-1-v1.log": {
                "is_compacted": false,
                "size_bytes": 10,
                "base_offset": 1,
                "committed_offset": 2
            }
        }
    })json";
    when().request(urls[0]).then_reply_with(manifest_json);
    when().request(urls[1]).then_reply_with(
      {archival_tests::error_payload.data(),
       archival_tests::error_payload.size()},
      ss::httpd::reply::status_type::not_found);
    when().request(urls[2]).then_reply_with(
      {archival_tests::error_payload.data(),
       archival_tests::error_payload.size()},
      ss::httpd::reply::status_type::not_found);
    add_topic_with_random_data(pid0, 20);
    add_topic_with_random_data(pid1, 20);
    wait_for_partition_leadership(pid0);
    wait_for_partition_leadership(pid1);

    auto& service = get_scheduler_service();

    service.reconcile_archivers().get();
    BOOST_REQUIRE(service.contains(pid0));
    BOOST_REQUIRE(service.contains(pid1));
}

FIXTURE_TEST(test_reconciliation_drop_ntp, archiver_fixture) {
    wait_for_controller_leadership().get();

    auto topic = model::topic("topic_2");
    auto ntp = model::ntp(test_ns, topic, model::partition_id(0));

    const char* url = "/50000000/meta/test-namespace/topic_2/0_2/manifest.json";
    const char* topic_url
      = "/20000000/meta/test-namespace/topic_2/topic_manifest.json";
    when().request(url).then_reply_with(
      {archival_tests::error_payload.data(),
       archival_tests::error_payload.size()},
      ss::httpd::reply::status_type::not_found);
    when().request(topic_url).then_reply_with(
      {archival_tests::error_payload.data(),
       archival_tests::error_payload.size()},
      ss::httpd::reply::status_type::not_found);

    add_topic_with_random_data(ntp, 20);

    wait_for_partition_leadership(ntp);

    auto& service = get_scheduler_service();

    service.reconcile_archivers().get();
    BOOST_REQUIRE(service.contains(ntp));

    // delete topic
    delete_topic(ntp.ns, ntp.tp.topic);

    wait_for_topic_deletion(ntp);

    service.reconcile_archivers().get();
    BOOST_REQUIRE(!service.contains(ntp));
}

FIXTURE_TEST(test_segment_upload, archiver_fixture) {
    wait_for_controller_leadership().get();

    auto topic = model::topic("topic_3");
    auto ntp = model::ntp(test_ns, topic, model::partition_id(0));

    model::revision_id partition_rev{get_next_partition_revision_id().get()};

    std::string manifest_ntp_path = fmt::format(
      "kafka/topic_3/0_{}", partition_rev);
    uint32_t hash = xxhash_32(
                      manifest_ntp_path.data(), manifest_ntp_path.size())
                    & 0xf0000000;
    std::string manifest_path = fmt::format(
      "/{:08x}/meta/{}/manifest.json", hash, manifest_ntp_path);

    archival::segment_name seg000{"0-0-v1.log"};
    archival::segment_name seg100{"100-0-v1.log"};

    listen();

    auto builder = get_started_log_builder(
      ntp, model::revision_id(partition_rev));
    using namespace storage; // NOLINT
    (*builder) | add_segment(model::offset(0))
      | add_random_batch(model::offset(0), 100, maybe_compress_batches::no)
      | add_segment(model::offset(100))
      | add_random_batch(model::offset(100), 100, maybe_compress_batches::no)
      | stop();
    vlog(
      arch_svc_log.trace,
      "{} bytes written to log {}",
      builder->bytes_written(),
      ntp.path());
    builder.reset();
    add_topic_with_archival_enabled(model::topic_namespace_view(ntp)).get();

    wait_for_partition_leadership(ntp);

    wait_for_lso(ntp);

    auto& service = get_scheduler_service();

    service.reconcile_archivers().get();
    BOOST_REQUIRE(service.contains(ntp));

    // 1 topic manifest, 1 partition manifest, 2 segments
    const size_t num_requests_expected = 4;
    tests::cooperative_spin_wait_with_timeout(10s, [this] {
        return get_requests().size() == num_requests_expected;
    }).get();
    BOOST_REQUIRE(get_requests().size() == num_requests_expected);

    cloud_storage::partition_manifest manifest;
    auto manifest_req = get_targets().equal_range(manifest_path);
    BOOST_REQUIRE(manifest_req.first != manifest_req.second);
    std::exception_ptr ex;
    auto it = manifest_req.first;
    BOOST_REQUIRE(it->second._method == "PUT");
    verify_manifest_content(it->second.content);
    manifest = load_manifest(it->second.content);

    auto seg000_meta = manifest.get(seg000);
    BOOST_REQUIRE(seg000_meta);
    ss::sstring seg000_url = "/"
                             + get_segment_path(manifest, seg000)().string();
    BOOST_REQUIRE(get_targets().count(seg000_url) == 1);
    auto put_seg000 = get_targets().find(seg000_url);
    BOOST_REQUIRE(put_seg000->second._method == "PUT");
    verify_segment(ntp, seg000, put_seg000->second.content);

    auto seg100_meta = manifest.get(seg100);
    BOOST_REQUIRE(seg100_meta);
    ss::sstring seg100_url = "/"
                             + get_segment_path(manifest, seg100)().string();
    BOOST_REQUIRE(get_targets().count(seg100_url) == 1);
    auto put_seg100 = get_targets().find(seg100_url);
    BOOST_REQUIRE(put_seg100->second._method == "PUT");
    verify_segment(ntp, seg100, put_seg100->second.content);
}