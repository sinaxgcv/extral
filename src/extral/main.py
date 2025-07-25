# Copyright 2025 Michael Anckaert
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from extral import __version__
from extral.config import Config, ConnectorConfig, TableConfig, FileItemConfig, DatabaseConfig
from extral.extract import extract_table
from extral.load import load_data
from extral.state import state
from extral.error_tracking import ErrorTracker
from extral.validation import PipelineValidator, format_validation_report

import argparse

logger = logging.getLogger(__name__)

DEFAULT_WORKER_COUNT = 4


def process_table(
    source_config: ConnectorConfig,
    destination_config: ConnectorConfig,
    dataset_config: TableConfig | FileItemConfig,
    pipeline_name: str,
    error_tracker: ErrorTracker,
) -> bool:
    """Process a single table/dataset. Returns True if successful, False otherwise."""
    start_time = time.time()
    try:
        logger.info(f"Processing dataset: {dataset_config.name}")

        # Extract phase
        try:
            file_path, schema_path = extract_table(
                source_config, dataset_config, pipeline_name
            )
            if file_path is None or schema_path is None:
                logger.info(
                    f"Skipping dataset load for '{dataset_config.name}' as there is no data extracted."
                )
                return True
        except Exception as e:
            duration = time.time() - start_time
            error_tracker.track_error(
                pipeline=pipeline_name,
                dataset=dataset_config.name,
                operation="extract",
                exception=e,
                duration_seconds=duration,
                include_stack_trace=True,
            )
            raise

        # Load phase
        try:
            load_data(
                destination_config,
                dataset_config,
                file_path,
                schema_path,
                pipeline_name,
            )
        except Exception as e:
            duration = time.time() - start_time
            error_tracker.track_error(
                pipeline=pipeline_name,
                dataset=dataset_config.name,
                operation="load",
                exception=e,
                duration_seconds=duration,
                include_stack_trace=True,
            )
            raise

        return True

    except Exception as e:
        logger.error(f"Error processing dataset '{dataset_config.name}': {e}")
        return False


def _setup_logging(args: argparse.Namespace):
    config = Config.read_config(args.config)
    logging_config = config.logging

    if logging_config.level == "debug":
        level = logging.DEBUG
    elif logging_config.level == "info":
        level = logging.INFO
    elif logging_config.level == "warning":
        level = logging.WARNING
    elif logging_config.level == "error":
        level = logging.ERROR
    elif logging_config.level == "critical":
        level = logging.CRITICAL
    else:
        logger.warning(
            f"Unknown logging level '{logging_config.level}', defaulting to INFO."
        )
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description=f"Extract and Load Data Tool (v{__version__})"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        default="config.yaml",
        help="Path to the configuration file. Defaults to 'config.yaml'.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show the version of the tool.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing even if some datasets fail.",
    )
    parser.add_argument(
        "--skip-datasets",
        type=str,
        nargs="+",
        help="Skip specified datasets during processing.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate the configuration without executing pipelines.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform validation and show execution plan without running pipelines.",
    )

    args = parser.parse_args()

    _setup_logging(args)

    logger.debug(f"Parsed arguments: {args} ")

    config_file_path = args.config
    run(
        config_file_path, 
        args.continue_on_error, 
        args.skip_datasets or [], 
        args.validate_only, 
        args.dry_run
    )


def run(
    config_file_path: str,
    continue_on_error: bool = False,
    skip_datasets: Optional[list[str]] = None,
    validate_only: bool = False,
    dry_run: bool = False,
):
    if skip_datasets is None:
        skip_datasets = []

    state.load_state()
    config = Config.read_config(config_file_path)
    
    # Perform pre-flight validation
    logger.info("Performing pre-flight validation...")
    validator = PipelineValidator()
    validation_report = validator.validate_configuration(config)
    
    # Print validation report
    print(format_validation_report(validation_report))
    
    # Handle validation-only mode
    if validate_only:
        logger.info("Validation complete. Exiting (--validate-only mode).")
        sys.exit(0 if validation_report.overall_valid else 1)
    
    # Check if validation failed
    if not validation_report.overall_valid:
        logger.error("Configuration validation failed. Cannot proceed with execution.")
        logger.error("Use --validate-only for detailed validation report.")
        sys.exit(1)
    
    # Handle dry-run mode
    if dry_run:
        logger.info("=== DRY RUN MODE ===")
        logger.info("Validation passed. Execution plan:")
        for pipeline in config.pipelines:
            logger.info(f"Pipeline: {pipeline.name}")
            if isinstance(pipeline.source, DatabaseConfig):
                logger.info(f"  Source: {pipeline.source.type} ({len(pipeline.source.tables)} tables)")
                for table in pipeline.source.tables:
                    logger.info(f"    - Table: {table.name} (strategy: {table.strategy.value})")
            else:  # FileConfig
                logger.info(f"  Source: {pipeline.source.type} ({len(pipeline.source.files)} files)")
                for file_item in pipeline.source.files:
                    file_name = file_item.file_path or file_item.http_path or "unknown"
                    logger.info(f"    - File: {file_name} (strategy: {file_item.strategy.value})")
            logger.info(f"  Destination: {pipeline.destination.type}")
            logger.info(f"  Workers: {pipeline.workers or DEFAULT_WORKER_COUNT}")
        logger.info("Dry run complete. Exiting (--dry-run mode).")
        sys.exit(0)

    if not config.pipelines:
        logger.error("No pipelines specified in the configuration.")
        sys.exit(1)

    # Initialize error tracker
    error_tracker = ErrorTracker()

    # Log configuration options
    if continue_on_error:
        logger.info("Running in continue-on-error mode")
    if skip_datasets:
        logger.info(f"Skipping datasets: {', '.join(skip_datasets)}")

    # Track overall statistics
    total_pipelines = len(config.pipelines)
    successful_pipelines = 0
    total_datasets = 0
    successful_datasets = 0

    # Process pipelines sequentially
    for pipeline in config.pipelines:
        logger.info(f"Processing pipeline: {pipeline.name}")
        pipeline_start = time.time()
        pipeline_success = True

        # Get worker count (pipeline-specific or global default)
        worker_count = (
            pipeline.workers or config.processing.workers or DEFAULT_WORKER_COUNT
        )

        # Get tables/datasets from the source configuration
        datasets: list[TableConfig | FileItemConfig] = []
        if hasattr(pipeline.source, "tables"):
            datasets = getattr(pipeline.source, "tables", [])
        elif hasattr(pipeline.source, "files"):
            datasets = getattr(pipeline.source, "files", [])

        if not datasets:
            logger.error(
                f"No datasets (tables or files) found in pipeline '{pipeline.name}'"
            )
            error_tracker.track_error(
                pipeline=pipeline.name,
                dataset="N/A",
                operation="pipeline_setup",
                exception=Exception("No datasets found in pipeline configuration"),
                duration_seconds=time.time() - pipeline_start,
            )
            continue

        logger.info(
            f"Found {len(datasets)} datasets to process in pipeline '{pipeline.name}'"
        )
        total_datasets += len(datasets)

        # Track datasets for this pipeline
        pipeline_dataset_success = 0

        # Filter out skipped datasets
        datasets_to_process = []
        for dataset in datasets:
            if dataset.name in skip_datasets:
                logger.info(f"Skipping dataset '{dataset.name}' as requested")
            else:
                datasets_to_process.append(dataset)

        if not datasets_to_process:
            logger.info(f"All datasets in pipeline '{pipeline.name}' were skipped")
            continue

        # Process datasets in parallel within the pipeline
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    process_table,
                    pipeline.source,
                    pipeline.destination,
                    dataset,
                    pipeline.name,
                    error_tracker,
                ): dataset
                for dataset in datasets_to_process
            }
            for future in as_completed(futures):
                dataset = futures[future]
                try:
                    success = future.result()
                    if success:
                        pipeline_dataset_success += 1
                        successful_datasets += 1
                        logger.info(
                            f"Completed processing dataset '{dataset.name}' in pipeline '{pipeline.name}'"
                        )
                    else:
                        pipeline_success = False
                except Exception as e:
                    pipeline_success = False
                    logger.error(
                        f"Error processing dataset '{dataset.name}' in pipeline '{pipeline.name}': {e}"
                    )
                    if not continue_on_error:
                        logger.error(
                            "Stopping execution due to error (use --continue-on-error to proceed)"
                        )
                        # Finalize report and exit
                        error_tracker.finalize_report(
                            total_pipelines=total_pipelines,
                            successful_pipelines=successful_pipelines,
                            total_datasets=total_datasets,
                            successful_datasets=successful_datasets,
                        )
                        logger.info("\n" + error_tracker.report.get_summary())
                        if error_tracker.report.errors:
                            error_report_path = Path("extral_error_report.json")
                            error_tracker.report.save_to_file(error_report_path)
                            logger.info(f"Error report saved to: {error_report_path}")
                        sys.exit(1)

        if pipeline_success and pipeline_dataset_success == len(datasets_to_process):
            successful_pipelines += 1
            logger.info(f"Successfully completed pipeline: {pipeline.name}")
        else:
            logger.warning(
                f"Pipeline '{pipeline.name}' completed with errors. "
                f"Successful datasets: {pipeline_dataset_success}/{len(datasets_to_process)}"
            )

    # Finalize error report
    error_tracker.finalize_report(
        total_pipelines=total_pipelines,
        successful_pipelines=successful_pipelines,
        total_datasets=total_datasets,
        successful_datasets=successful_datasets,
    )

    # Display error summary
    logger.info("\n" + error_tracker.report.get_summary())

    # Save error report if there were errors
    if error_tracker.report.errors:
        error_report_path = Path("extral_error_report.json")
        error_tracker.report.save_to_file(error_report_path)
        logger.info(f"Error report saved to: {error_report_path}")

    # Store state
    state.store_state()

    # Exit with error code if there were failures
    if (
        error_tracker.report.failed_pipelines > 0
        or error_tracker.report.failed_datasets > 0
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
