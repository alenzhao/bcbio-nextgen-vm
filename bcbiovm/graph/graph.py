from __future__ import print_function

import calendar
from datetime import datetime
import os
import re

import matplotlib
matplotlib.use('Agg')
import pylab
pylab.rcParams['figure.figsize'] = (35.0, 12.0)
import pandas as pd

from bcbio import utils
from bcbiovm.graph.collectl import load_collectl
from bcbiovm.graph.elasticluster import fetch_collectl


def get_bcbio_timings(path):
    """Fetch timing information from a bcbio log file."""
    with open(path, 'r') as fp:
        steps = {}
        for line in fp:
            matches = re.search(r'^\[([^\]]+)\] ([^:]+: .*)', line)
            if not matches:
                continue

            tstamp = matches.group(1)
            msg = matches.group(2)

            # FIXME: have bcbio send a start event.
            #if 'Using input YAML configuration:' in msg:
            #    when = datetime.strptime(tstamp, '%Y-%m-%d %H:%M')
            #    steps[when] = 'Starting'
            #    continue

            if not msg.find('Timing: ') >= 0:
                continue

            when = datetime.strptime(tstamp, '%Y-%m-%d %H:%M')
            step = msg.split(":")[-1].strip()
            steps[when] = step

        return steps


def this_and_prev(iterable):
    """Walk an iterable, returning the current and previous items
    as a two-tuple."""
    try:
        item = next(iterable)
        while True:
            next_item = next(iterable)
            yield item, next_item
            item = next_item
    except StopIteration:
        return


def calc_deltas(df, series=[]):
    """Many of collectl's data values are cumulative (monotonically
    increasing), so subtract the previous value to determine the value
    for the current interval.
    """
    df = df.sort(ascending=False)

    prev = 0
    for this, prev in this_and_prev(iter(df.index)):
        for s in series:
            if df[s][this] < df[s][prev]:
                # A data source that should be increasing
                # monotonically has been reset, so the host
                # must have rebooted.
                # FIXME: trash this sample?
                df[s][this] = 0
                continue
            # Take the difference from the previous value and
            # divide by the interval since the previous sample,
            # so we always return values in units/second.
            df[s][this] = (df[s][this] - df[s][prev]) / (this - prev).seconds
    for s in series:
        df[s][prev] = 0

    return df


def remove_outliers(series, stddev):
    """Remove the outliers from a series."""
    return series[(series - series.mean()).abs() < stddev * series.std()]


def prep_for_graph(df, series=[], delta_series=[], smoothing=None,
                   outlier_stddev=None):
    """Prepare a dataframe for graphing by calculating deltas for
    series that need them, resampling, and removing outliers.
    """
    graph = calc_deltas(df, delta_series)

    for s in series + delta_series:
        if smoothing:
            graph[s] = graph[s].resample(smoothing)
        if outlier_stddev:
            graph[s] = remove_outliers(graph[s], outlier_stddev)

    return graph[series + delta_series]


def add_common_plot_features(plot, steps):
    """Add plot features common to all plots, such as bcbio step
    information.
    """
    plot.yaxis.set_tick_params(labelright=True)
    plot.set_xlabel('')

    ylim = plot.get_ylim()
    ticks = {}
    for tstamp, step in steps.iteritems():
        if step == 'finished':
            continue
        plot.vlines(tstamp, *ylim, linestyles='dashed')
        ticks[tstamp] = step
    tick_kvs = sorted(ticks.iteritems())
    top_axis = plot.twiny()
    top_axis.set_xlim(*plot.get_xlim())
    top_axis.set_xticks([k for k, v in tick_kvs])
    top_axis.set_xticklabels([v for k, v in tick_kvs], rotation=45, ha='left')
    plot.set_ylim(0)

    return plot


def graph_cpu(df, steps, num_cpus):
    graph = prep_for_graph(
        df, delta_series=['cpu_user', 'cpu_sys', 'cpu_wait'])

    graph['cpu_user'] /= num_cpus
    graph['cpu_sys'] /= num_cpus
    graph['cpu_wait'] /= num_cpus

    plot = graph.plot()
    plot.set_ylabel('% CPU')
    add_common_plot_features(plot, steps)

    return plot


def graph_net_bytes(df, steps, ifaces):
    series = []
    for iface in ifaces:
        series.extend(['{}_rbyte'.format(iface), '{}_tbyte'.format(iface)])

    graph = prep_for_graph(df, delta_series=series)

    for iface in ifaces:
        old_series = '{}_rbyte'.format(iface)
        new_series = '{}_receive'.format(iface)
        graph[new_series] = graph[old_series] * 8 / 1024 / 1024
        del graph[old_series]

        old_series = '{}_tbyte'.format(iface)
        new_series = '{}_transmit'.format(iface)
        graph[new_series] = graph[old_series] * 8 / 1024 / 1024
        del graph[old_series]

    plot = graph.plot()
    plot.set_ylabel('mbits/s')
    add_common_plot_features(plot, steps)

    return plot


def graph_net_pkts(df, steps, ifaces):
    series = []
    for iface in ifaces:
        series.extend(['{}_rpkt'.format(iface), '{}_tpkt'.format(iface)])

    graph = prep_for_graph(df, delta_series=series)

    plot = graph.plot()
    plot.set_ylabel('packets/s')
    add_common_plot_features(plot, steps)

    return plot


def graph_memory(df, steps):
    graph = prep_for_graph(
        df, series=['mem_total', 'mem_free', 'mem_buffers', 'mem_cached'])

    free_memory = graph['mem_free'] + graph['mem_buffers'] + \
        graph['mem_cached']
    graph = (graph['mem_total'] - free_memory) / 1024 / 1024

    plot = graph.plot()
    plot.set_ylabel('gbytes')
    add_common_plot_features(plot, steps)

    return plot


def graph_disk_io(df, steps, disks):
    series = []
    for disk in disks:
        series.extend([
            '{}_sectors_read'.format(disk),
            '{}_sectors_written'.format(disk),
        ])

    graph = prep_for_graph(df, delta_series=series, outlier_stddev=2)

    for disk in disks:
        old_series = '{}_sectors_read'.format(disk)
        new_series = '{}_read'.format(disk)
        graph[new_series] = graph[old_series]
        del graph[old_series]

        old_series = '{}_sectors_written'.format(disk)
        new_series = '{}_write'.format(disk)
        graph[new_series] = graph[old_series]
        del graph[old_series]

    plot = graph.plot()
    plot.set_ylabel('sectors/s')
    add_common_plot_features(plot, steps)

    return plot


def generate_graphs(collectl_datadir, bcbio_log_path, outdir, verbose=False):
    """Generate all graphs for a bcbio run."""
    if verbose:
        print('Reading timings from bcbio log...')
    steps = get_bcbio_timings(bcbio_log_path)
    start_time = min(steps.keys()).strftime('%Y-%m-%d %H:%M:%S')
    end_time = max(steps.keys()).strftime('%Y-%m-%d %H:%M:%S')

    dfs = {}
    for item in sorted(os.listdir(collectl_datadir)):
        if not item.endswith('.raw.gz'):
            continue

        if verbose:
            print('Loading performance data from {}...'.format(item))
        df, hardware = load_collectl(os.path.join(collectl_datadir, item))
        df = df[start_time:end_time]

        host = item.split('-')[0]
        if host not in dfs:
            dfs[host] = df
        else:
            old_df = dfs[host]
            dfs[host] = pd.concat([old_df, df])

    for host, df in dfs.iteritems():
        if verbose:
            print('Generating CPU graph for {}...'.format(host))
        graph = graph_cpu(df, steps, hardware['num_cpus'])
        graph.get_figure().savefig(
            os.path.join(outdir, '{}_cpu.png'.format(host)),
            bbox_inches='tight', pad_inches=0.25)
        pylab.close()

        ifaces = set([
            series.split('_')[0]
            for series
             in df.keys()
             if series.startswith('eth')
        ])

        if verbose:
            print('Generating network graphs for {}...'.format(host))
        graph = graph_net_bytes(df, steps, ifaces)
        graph.get_figure().savefig(
            os.path.join(outdir, '{}_net_bytes.png'.format(host)),
            bbox_inches='tight', pad_inches=0.25)
        pylab.close()

        graph = graph_net_pkts(df, steps, ifaces)
        graph.get_figure().savefig(
            os.path.join(outdir, '{}_net_pkts.png'.format(host)),
            bbox_inches='tight', pad_inches=0.25)
        pylab.close()

        if verbose:
            print('Generating memory graph for {}...'.format(host))
        graph = graph_memory(df, steps)
        graph.get_figure().savefig(
            os.path.join(outdir, '{}_memory.png'.format(host)),
            bbox_inches='tight', pad_inches=0.25)
        pylab.close()

        if verbose:
            print('Generating storage I/O graph for {}...'.format(host))
        drives = set([
            series.split('_')[0]
            for series
             in df.keys()
             if series.startswith(('sd', 'vd', 'hd', 'xvd'))
        ])
        graph = graph_disk_io(df, steps, drives)
        graph.get_figure().savefig(
            os.path.join(outdir, '{}_disk_io.png'.format(host)),
            bbox_inches='tight', pad_inches=0.25)
        pylab.close()


def bootstrap(args):
    if args.cluster and args.cluster.lower() not in ["none", "false"]:
        fetch_collectl(args.econfig, args.cluster,
            utils.safe_makedir(args.rawdir), args.verbose)
    generate_graphs(args.rawdir, args.log, utils.safe_makedir(args.outdir),
        verbose=args.verbose)
