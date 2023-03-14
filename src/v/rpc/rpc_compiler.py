#!/usr/bin/env python3
# Copyright 2020 Redpanda Data, Inc.
#
# Use of this software is governed by the Business Source License
# included in the file licenses/BSL.md
#
# As of the Change Date specified in that file, in accordance with
# the Business Source License, use of this software will be governed
# by the Apache License, Version 2.0

import sys
import os
import logging
import json

# 3rd party
from jinja2 import Template
import zlib

sys.path.append(os.path.dirname(__file__))
logger = logging.getLogger('rp')

RPC_TEMPLATE = """
// This file is autogenerated. Manual changes will be lost.
#pragma once

#include "config/configuration.h"
#include "reflection/adl.h"
#include "rpc/types.h"
#include "rpc/parse_utils.h"
#include "rpc/transport.h"
#include "rpc/service.h"
#include "finjector/hbadger.h"
#include "utils/string_switch.h"
#include "random/fast_prng.h"
#include "outcome.h"
#include "prometheus/prometheus_sanitize.h"
#include "seastarx.h"

// extra includes
{%- for include in includes %}
#include "{{include}}"
{%- endfor %}

#include <seastar/core/metrics.hh>
#include <seastar/core/reactor.hh>
#include <seastar/core/sleep.hh>
#include <seastar/core/scheduling.hh>

#include <functional>
#include <chrono>
#include <tuple>
#include <cstdint>

namespace {{namespace}} {

template<typename Codec>
class {{service_name}}_service_base : public rpc::service {
public:
    class failure_probes;

    {% for method in methods %}
    static constexpr rpc::method_info {{method.name}}_method = {"{{service_name}}::{{method.name}}", {{method.id}}};
    {%- endfor %}

    {{service_name}}_service_base(ss::scheduling_group sc, ss::smp_service_group ssg)
       : _sc(sc), _ssg(ssg) {}

    {{service_name}}_service_base({{service_name}}_service_base&& o) noexcept
      : _sc(std::move(o._sc)), _ssg(std::move(o._ssg)), _methods(std::move(o._methods)) {}

    {{service_name}}_service_base& operator=({{service_name}}_service_base&& o) noexcept {
       if(this != &o){
          this->~{{service_name}}_service_base();
          new (this) {{service_name}}_service_base(std::move(o));
       }
       return *this;
    }

    virtual ~{{service_name}}_service_base() noexcept = default;

    void setup_metrics() final {
        namespace sm = ss::metrics;
        auto service_label = sm::label("service");
        auto method_label = sm::label("method");
      {%- for method in methods %}
        {
            std::vector<ss::metrics::label_instance> labels{
              service_label("{{service_name}}"),
              method_label("{{method.name}}")};
                auto aggregate_labels
                  = config::shard_local_cfg().aggregate_metrics()
                      ? std::vector<sm::label>{sm::shard_label, method_label}
                      : std::vector<sm::label>{};
            _metrics.add_group(
              prometheus_sanitize::metrics_name("internal_rpc"),
              {sm::make_histogram(
                "latency",
                [this] { return _methods[{{loop.index-1}}].probes.latency_hist().seastar_histogram_logform(); },
                sm::description("Internal RPC service latency"),
                labels)
                .aggregate(aggregate_labels)});
        }
      {%- endfor %}
    }

    ss::scheduling_group& get_scheduling_group() override {
       return _sc;
    }

    ss::smp_service_group& get_smp_service_group() override {
       return _ssg;
    }

    rpc::method* method_from_id(uint32_t id) final {
       switch(id) {
       {%- for method in methods %}
         case {{method.id}}: return &_methods[{{loop.index - 1}}];
       {%- endfor %}
         default: return nullptr;
       }
    }
    {%- for method in methods %}
    /// \\brief {{method.input_type}} -> {{method.output_type}}
    virtual ss::future<rpc::netbuf>
    raw_{{method.name}}(ss::input_stream<char>& in, rpc::streaming_context& ctx) {
      return execution_helper<{{method.input_type}},
                              {{method.output_type}},
                              Codec>::exec(in, ctx, {{method.name}}_method,
      [this](
          {{method.input_type}}&& t, rpc::streaming_context& ctx) -> ss::future<{{method.output_type}}> {
          return {{method.name}}(std::move(t), ctx);
      });
    }
    virtual ss::future<{{method.output_type}}>
    {{method.name}}({{method.input_type}}&&, rpc::streaming_context&) {
       throw std::runtime_error("unimplemented method");
    }
    {%- endfor %}
private:
    ss::scheduling_group _sc;
    ss::smp_service_group _ssg;
    std::array<rpc::method, {{methods|length}}> _methods{%raw %}{{{% endraw %}
      {%- for method in methods %}
      rpc::method([this] (ss::input_stream<char>& in, rpc::streaming_context& ctx) {
         return raw_{{method.name}}(in, ctx);
      }){{ "," if not loop.last }}
      {%- endfor %}
    {% raw %}}}{% endraw %};
    ss::metrics::metric_groups _metrics;
};

using {{service_name}}_service = {{service_name}}_service_base<rpc::default_message_codec>;

class {{service_name}}_client_protocol {
public:
    explicit {{service_name}}_client_protocol(rpc::transport& t)
      : _transport(t) {
    }

    virtual ~{{service_name}}_client_protocol() = default;

    {%- for method in methods %}
    virtual inline ss::future<result<rpc::client_context<{{method.output_type}}>>>
    {{method.name}}({{method.input_type}}&& r, rpc::client_opts opts) {
       return _transport.send_typed<{{method.input_type}}, {{method.output_type}}>(std::move(r),
              {{service_name}}_service::{{method.name}}_method, std::move(opts));
    }
    {%- endfor %}

private:
    rpc::transport& _transport;
};

template<typename Codec>
class {{service_name}}_service_base<Codec>::failure_probes final : public finjector::probe {
public:
    using type = uint32_t;

    static constexpr std::string_view name() { return "{{service_name}}_service::failure_probes"; }

    enum class methods: type {
    {%- for method in methods %}
        {{method.name}} = 1 << {{loop.index}}{{ "," if not loop.last }}
    {%- endfor %}
    };
    type point_to_bit(std::string_view point) const final {
        return string_switch<type>(point)
        {%- for method in methods %}
          .match("{{method.name}}", static_cast<type>(methods::{{method.name}}))
        {%- endfor %}
          .default_match(0);
    }
    std::vector<std::string_view> points() final {
        std::vector<std::string_view> retval;
        retval.reserve({{methods | length}});
        {%- for method in methods %}
        retval.push_back("{{method.name}}");
        {%- endfor %}
        return retval;
    }
    {%- for method in methods %}
    ss::future<> {{method.name}}() {
        if(is_enabled()) {
          return do_{{method.name}}();
        }
        return ss::make_ready_future<>();
    }
    {%- endfor %}
private:
    {%- for method in methods %}
    [[gnu::noinline]] ss::future<> do_{{method.name}}() {
        if (_exception_methods & type(methods::{{method.name}})) {
          return ss::make_exception_future<>(std::runtime_error(
            "FailureInjector: "
            "{{namespace}}::{{service_name}}::{{method.name}}"));
        }
        if (_delay_methods & type(methods::{{method.name}})) {
            return ss::sleep(std::chrono::milliseconds(_prng() % 50));
        }
        if (_termination_methods & type(methods::{{method.name}})) {
            std::terminate();
        }
        return ss::make_ready_future<>();
    }
    {%- endfor %}

    fast_prng _prng;
};

} // namespace
"""


def _read_file(name):
    with open(name, 'r') as f:
        return json.load(f)


def _enrich_methods(service):
    logger.info(service)

    service["id"] = zlib.crc32(
        bytes("%s:%s" % (service["namespace"], service["service_name"]),
              "utf-8"))

    def _xor_id(m):
        mid = ("%s:" % service["namespace"]).join(
            [m["name"], m["input_type"], m["output_type"]])
        return service["id"] ^ zlib.crc32(bytes(mid, 'utf-8'))

    for m in service["methods"]:
        m["id"] = _xor_id(m)

    return service


def _codegen(service, out):
    logger.info(service)
    tpl = Template(RPC_TEMPLATE)
    with open(out, 'w') as f:
        f.write(tpl.render(service))


def main():
    import argparse

    def generate_options():
        parser = argparse.ArgumentParser(description='service codegenerator')
        parser.add_argument(
            '--log',
            type=str,
            default='INFO',
            help='info,debug, type log levels. i.e: --log=debug')
        parser.add_argument('--service_file',
                            type=str,
                            help='input file in .json format for the codegen')
        parser.add_argument('--output_file',
                            type=str,
                            default='/dev/stderr',
                            help='output header file for the codegen')
        return parser

    parser = generate_options()
    options, program_options = parser.parse_known_args()
    logger.info("%s" % options)
    _codegen(_enrich_methods(_read_file(options.service_file)),
             options.output_file)


if __name__ == '__main__':
    main()