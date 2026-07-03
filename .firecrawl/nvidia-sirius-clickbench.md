[Technical Blog](https://developer.nvidia.com/blog)

[Subscribe](https://developer.nvidia.com/email-signup)

[Related Resources](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/#main-content-end)

[Data Science](https://developer.nvidia.com/blog/category/data-science/)

English中文

# NVIDIA CUDA-X Powers the New Sirius GPU Engine for DuckDB, Setting ClickBench Records

![Decorative image.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/ClickBench-Sirius-1024x576-jpg.webp)

Dec 15, 2025


By [Xiangyao Yu](https://developer.nvidia.com/blog/author/xingyaoyu/ "Posts by Xiangyao Yu"), [Bobbi Yogatama](https://developer.nvidia.com/blog/author/byogatama/ "Posts by Bobbi Yogatama"), [Yifei Yang](https://developer.nvidia.com/blog/author/yyang/ "Posts by Yifei Yang") and [Rodrigo Aramburu](https://developer.nvidia.com/blog/author/raramburu/ "Posts by Rodrigo Aramburu")

+16

Like

[Discuss (0)](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/#entry-content-comments)

- [L](https://www.linkedin.com/sharing/share-offsite/?url=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [T](https://twitter.com/intent/tweet?text=NVIDIA+CUDA-X+Powers+the+New+Sirius+GPU+Engine+for+DuckDB%2C+Setting+ClickBench+Records+%7C+NVIDIA+Technical+Blog+https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [F](https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [R](https://www.reddit.com/submit?url=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F&title=NVIDIA+CUDA-X+Powers+the+New+Sirius+GPU+Engine+for+DuckDB%2C+Setting+ClickBench+Records+%7C+NVIDIA+Technical+Blog)
- [E](mailto:?subject=I%27d%20like%20to%20share%20a%20link%20with%20you&body=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)

AI-Generated Summary

Like

Dislike

- NVIDIA and the University of Wisconsin-Madison developed Sirius, a GPU-native execution engine that accelerates DuckDB analytics by leveraging NVIDIA CUDA-X libraries, such as cuDF and RAPIDS Memory Manager, without requiring changes to DuckDB’s codebase.
- Sirius achieved record-breaking performance and cost-efficiency on ClickBench benchmarks by executing SQL operations on GPUs, surpassing CPU-based competitors in both speed and total cost of ownership, and demonstrating notable speedups in queries involving filtering, projections, aggregations, and string operations.
- Future development goals for Sirius include enhanced GPU memory management, GPU-native file readers with prefetching, a pipeline-oriented execution model, and a scalable multi-node, multi-GPU architecture to support advanced analytics workloads.

AI-generated content may summarize information incompletely. Verify important information. [Learn more](https://www.nvidia.com/en-us/agreements/trustworthy-ai/terms/)

Sirius, an open-source GPU native SQL engine, achieved a new performance record on Clickbench—a widely used analytics benchmark. Developed by University of Wisconsin-Madison with support from NVIDIA engineers, Sirius brings GPU-accelerated analytics to DuckDB.

DuckDB has seen rapid adoption among organizations such as DeepSeek, Microsoft, and Databricks due to its simplicity, speed, and versatility. As analytics workloads are highly amenable to massive parallelism, GPUs have emerged as the natural next step with higher performance, throughput, and better total cost of ownership (TCO) compared to CPU-based databases. However, this growing demand for GPU acceleration is hindered by the challenge of building a database system from the ground up.

This is solved with the jointly developed **Sirius**, a composable GPU-native execution backend for DuckDB that reuses its advanced subsystems while accelerating query execution with GPUs. Using NVIDIA CUDA-X libraries, Sirius delivers GPU acceleration.

This blog post outlines the Sirius architecture and demonstrates how it achieved **record-breaking performance** on ClickBench, a widely used analytics benchmark.

## Sirius: A GPU-native SQL engine [Scroll to Sirius: A GPU-native SQL engine section](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/\#sirius_a_gpu-native_sql_engine)

![Diagram of the Sirius GPU-native SQL engine architecture, showing multiple query engines feeding a shared Substrait query plan executed on NVIDIA GPU libraries, with connections to local and cloud storage.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Sirius-Architecture-png.webp)_Figure 1. Sirius architecture_

Sirius is a GPU-native SQL engine that provides drop-in acceleration for DuckDB—and, in the future, other data systems.

The team recently published [an article](https://arxiv.org/abs/2508.04701) detailing the Sirius architecture and demonstrated state-of-the-art performance on TPC-H at SF100.

Implemented as a DuckDB extension, Sirius requires no modifications to DuckDB’s codebase and only minimal changes to the user-facing interface. At the execution boundary, Sirius consumes query plans in the universal **Substrait** format, ensuring compatibility with other data systems. To minimize engineering effort and maximize reliability, Sirius is built on well-established NVIDIAlibraries:

- **NVIDIA cuDF:** High-performance, columnar-oriented relational operators (e.g., joins, aggregations, projections) natively designed for GPUs.
- **NVIDIA RAPIDS Memory Manager (RMM):** An efficient GPU memory allocator, reducing fragmentation and allocation overheads.

Sirius constructs its GPU-native execution engine and buffer management on top of these high-performance libraries, while reusing DuckDB’s advanced subsystems —including its query parser, optimizer, and scan operators, where appropriate. This combination of mature ecosystems gives Sirius a head start, enabling it to break the ClickBench record with minimal engineering effort.

![Diagram of a Sirius query where DuckDB scans a table, converts data to Apache Arrow, and NVIDIA cuDF executes aggregates and projections on the GPU.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Query-png.webp)_Figure 2. Sirius Query on CPU and GPUs_

As illustrated in Figure 2, the process begins when Sirius receives an already optimized query plan from DuckDB’s internal format, ensuring robust logical and physical optimizations are preserved. For table scans, Sirius invokes DuckDB’s scan functionality, which provides features such as min-max filtering, zone skipping, and on-the-fly decompression—these operations efficiently load the relevant data into host memory.

Next, the result of the table scan is transformed from DuckDB’s native format into a Sirius data format (closely aligned with Apache Arrow), which is then transferred to GPU memory. In benchmarks like ClickBench, Sirius can cache frequently accessed tables on the GPU, accelerating repeated query execution.

The Sirius format can be mapped directly to a cudf::table for zero-copy interoperability, enabling all remaining SQL operators (aggregates, projections, and joins) to execute at GPU speed through cuDF primitives. Once computation completes, results are transferred back to the CPU, converted to DuckDB’s expected output format, and returned to the user—offering both raw speed and a seamless, familiar analytics experience.

## Hitting \#1 on Clickbench [Scroll to Hitting \#1 on Clickbench  section](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/\#hitting_\#1_on_clickbench%C2%A0)

Sirius running on an [NVIDIA GH200 Grace Hopper Superchip](https://resources.nvidia.com/en-us-data-center-overview-mc/en-us-data-center-overview/grace-hopper-superchip-datasheet-partner) instance from Lambda Labs ($1.5/hour) was evaluated against the top five systems on ClickBench. The alternative systems ran on CPU-only instances—AWS c6a.metal ($7.3/hour), AWS c8g.metal-48xl ($7.6/hour), and AWS c7a.metal-48xl ($9.8/hour). Hot-run execution time and relative runtime are reported, following the ClickBench methodologies, where lower values indicate better performance, and _1.0 represents the best possible score_. Figure 3 shows the geometric mean of the relative runtime across all benchmark queries. In the ClickBench runs, Sirius achieved the lowest relative runtime on cheaper hardware, resulting in at least 7.2x higher cost-efficiency under this setup. Note that these benchmark results were obtained at the time of evaluation and are subject to change in the future.

![Bar chart of ClickBench overall performance and cost, showing Sirius (lambda-GH200) as the fastest and lowest-cost system compared with Umbra, DuckDB, and Salesforce Hyper.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Cost-Runtime-png.webp)_Figure 3. ClickBench cost and relative runtime_

Figure 4 shows the hot-run query performance in Sirius and the top two systems in ClickBench: Umbra and DuckDB. Sirius achieved the lowest relative runtime in most queries, driven by efficient GPU computation through cuDF. For instance, in q4, q5, and q18, Sirius shows substantial performance gains on commonly used operators such as filtering, projection, and aggregation.

A few queries, however, reveal opportunities for further improvement. For example, q23 is bottlenecked by the “contains” operation on string columns, q24 and q26 by top-N operators, and q27 by aggregation over huge inputs. Future versions of Sirius will include continual improvements to these operators.

![Grouped bar chart of ClickBench relative runtimes per query, comparing Umbra, DuckDB, and Sirius, with Sirius generally showing the lowest runtime across most queries.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Runtime-png.webp)_Figure 4. Relative Runtime of Individual ClickBench Queries_

Figure 5 is a closer look at one of the most complex ClickBench queries, the regular expression query (q28). When implemented naively, regular expression matching on GPUs can produce massive kernels with high register pressure and complex control flow, leading to severe performance degradation.

To address this, Sirius leverages [cuDF’s JIT-compiled string transformation framework](https://developer.nvidia.com/blog/efficient-transforms-in-cudf-using-jit-compilation/) for user-defined functions. Figure 5 compares the performance of the JIT approach to cuDF’s precompiled API (cudf::strings::replace\_with\_backrefs), showing a 13x speedup.

The JIT-transformed kernel achieves 85% warp occupancy, compared to only 32% for the precompiled version, demonstrating better GPU utilization. By decomposing the regular expression into standard string operations such as character comparisons and substring operations, the cuDF JIT framework can fuse these operations into a single kernel, improving data locality and reducing register pressure.

![Horizontal bar chart of ClickBench Q28 execution time showing Sirius with JIT-compiled transform running much faster than precompiled Sirius, DuckDB, and Umbra.](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Performance-png.webp)_Figure 5. Performance comparison of Sirius on Q28 using JIT-compiled transform vs. precompiled regular expression_

## What’s next for Sirius [Scroll to What’s next for Sirius section](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/\#what%E2%80%99s_next_for_sirius)

Looking ahead, Sirius plans to integrate new foundational, shareable building blocks for GPU data processing being developed by NVIDIA. These building blocks are guided by the modular, interoperable, composable, extensible (MICE) principles described in the [Composable Codex](http://voltrondata.com/codex). Priority areas include:

- **Advanced GPU memory management:** Developing robust strategies to manage GPU memory efficiently, including seamless spilling of data beyond physical GPU limits to maintain performance and scale.
- **GPU file readers and intelligent I/O prefetching:** Plugging in GPU-native file readers with smart prefetching to accelerate data loading, minimize stalls, and reduce I/O bottlenecks.
- **Pipeline-oriented execution model:** Evolving Sirius’s core to a fully composable pipeline architecture that streamlines data flows across GPUs, host, and disk, efficiently overlapping computation and communication while enabling plug-and-play interoperability with open standards.
- **Scalable multi-node, multi-GPU architecture:** Expanding Sirius’s capability to scale out efficiently to multiple nodes and multiple GPUs, unlocking petabyte-scaled data processing.

By investing in these MICE-compliant components, Sirius aims to make GPU analytics engines easier to build, integrate, and extend—not just for Sirius, but for the entire open-source analytics ecosystem.

## Join Sirius [Scroll to Join Sirius  section](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/\#join_sirius%C2%A0)

Sirius is open source with the permissive Apache License 2.0. Led by the University of Wisconsin-Madison with support from NVIDIA, the project welcomes contributions from researchers and practitioners with the shared mission of driving the GPU era in data analytics.

We invite you to:

- Try Sirius on [ClickBench](https://benchmark.clickhouse.com/#system=-&type=-&machine=-ca2l%7C6t%7Cg4e%7C6ax%7C6ale%7C3al&cluster_size=-&opensource=-&hardware=-&tuned=+n&metric=hot&queries=-).
- Explore our [GitHub repo](https://github.com/sirius-db/sirius).
- Check out [Rethinking Analytical Processing in the GPU Era](https://arxiv.org/abs/2508.04701) and learn more at CIDR 2026.
- Join the [Sirius Slack community](https://join.slack.com/t/sirius-db/shared_invite/zt-33tuwt1sk-aa2dk0EU_dNjklSjIGW3vg)

[Discuss (0)](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/#entry-content-comments)

+16

Like

## Tags

[Data Science](https://developer.nvidia.com/blog/category/data-science/) \| [General](https://developer.nvidia.com/blog/recent-posts/?industry=General) \| [cuDF](https://developer.nvidia.com/blog/recent-posts/?products=cuDF) \| [Intermediate Technical](https://developer.nvidia.com/blog/recent-posts/?learning_levels=Intermediate+Technical) \| [Benchmark](https://developer.nvidia.com/blog/recent-posts/?content_types=Benchmark) \| [CUDA-X](https://developer.nvidia.com/blog/tag/cuda-x/) \| [Data Analytics / Processing](https://developer.nvidia.com/blog/tag/accelerated-data-analytics/) \| [featured](https://developer.nvidia.com/blog/tag/featured/)

## About the Authors

![Avatar photo](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Xingyao-Yu-262x262.jpg)

**About Xiangyao Yu**


Xiangyao Yu is an assistant professor of Computer Sciences at the University of Wisconsin-Madison, specializing in database systems. His research primarily focuses on GPU-accelerated databases, cloud-native databases, and high-performance SQL analytics and transaction processing. Before joining UW-Madison, he finished postdoc and PhD at MIT. Xiangyao is a recipient of the NSF CAREER Award, the Sloan Research Fellowship, and the VLDB Early Career Research Contribution Award.




[View all posts by Xiangyao Yu](https://developer.nvidia.com/blog/author/xingyaoyu/)

![Avatar photo](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Bobbi-Yogatama-262x262.jpg)

**About Bobbi Yogatama**


Bobbi Yogatama works on GPU-accelerated data processing at NVIDIA, where he is a core contributor to Sirius. Bobbi holds a PhD in Computer Science from UW–Madison, where he worked on GPU databases with Prof. Xiangyao Yu.




[View all posts by Bobbi Yogatama](https://developer.nvidia.com/blog/author/byogatama/)

![Avatar photo](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/Yifei-Yang-262x262.jpg)

**About Yifei Yang**


Yifei Yang is a final-year PhD candidate at the University of Wisconsin–Madison. His research focuses on query optimization for OLAP workloads, cloud-native databases, and GPU-native query engines.




[View all posts by Yifei Yang](https://developer.nvidia.com/blog/author/yyang/)

![Avatar photo](https://developer-blogs.nvidia.com/wp-content/uploads/2025/12/cropped-Rodrigo-Aramburu-262x262.jpg)

**About Rodrigo Aramburu**


Rodrigo Aramburu leads Developer Relations for accelerated data processing at NVIDIA, helping developers adopt and innovate with GPU-accelerated analytics. Previously, he co-founded BlazingSQL and Voltron Data, contributing to the RAPIDS ecosystem and advancing Arrow-native data systems.




[View all posts by Rodrigo Aramburu](https://developer.nvidia.com/blog/author/raramburu/)

## Comments

### Start the discussion at [forums.developer.nvidia.com](https://forums.developer.nvidia.com/t/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/354751)

- [![](https://developer-blogs.nvidia.com/wp-content/uploads/2026/06/gtc26-berlin-open-reg-mktg-kit-tech-blog-1360x180-1.webp)](https://www.nvidia.com/gtc/)
- [![](https://developer-blogs.nvidia.com/wp-content/uploads/2026/06/Copy-of-siggraph26-email-footer-1360x180-1.webp)](https://www.nvidia.com/en-us/events/siggraph)

ClosePrevious

![](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/)

![](https://developer.nvidia.com/blog/nvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record/)

Next

- [L](https://www.linkedin.com/sharing/share-offsite/?url=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [T](https://twitter.com/intent/tweet?text=NVIDIA+CUDA-X+Powers+the+New+Sirius+GPU+Engine+for+DuckDB%2C+Setting+ClickBench+Records+%7C+NVIDIA+Technical+Blog+https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [F](https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)
- [R](https://www.reddit.com/submit?url=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F&title=NVIDIA+CUDA-X+Powers+the+New+Sirius+GPU+Engine+for+DuckDB%2C+Setting+ClickBench+Records+%7C+NVIDIA+Technical+Blog)
- [E](mailto:?subject=I%27d%20like%20to%20share%20a%20link%20with%20you&body=https%3A%2F%2Fdeveloper.nvidia.com%2Fblog%2Fnvidia-gpu-accelerated-sirius-achieves-record-setting-clickbench-record%2F)

- [Join](https://developer.nvidia.com/login)