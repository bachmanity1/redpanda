// Copyright 2023 Redpanda Data, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/*
Package redpanda is the SDK for Redpanda's inline Data Transforms, based on WebAssembly.

This library provides a framework for transforming records written within Redpanda from
an input to an output topic.

Schema registry users can interact with schema registry using a [built-in client].

[built-in client]: https://pkg.go.dev/github.com/redpanda-data/redpanda/src/go/sdk/sr
*/
package redpanda
