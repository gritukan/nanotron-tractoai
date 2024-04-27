import numpy as np
import pytest
from helpers.context import TestContext
from helpers.data import (
    assert_batch_dataloader,
    assert_nanoset_sync_across_all_ranks,
    compute_batch_hash,
    create_dataset_paths,
    create_dummy_json_dataset,
    preprocess_dummy_dataset,
)
from helpers.utils import available_gpus, get_all_3d_configurations, init_distributed, rerun_if_address_is_in_use
from nanotron.data.dataloader_builder import build_nanoset_dataloader
from nanotron.data.nanoset import Nanoset
from nanotron.parallel import ParallelContext
from nanotron.utils import main_rank_first


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 4) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
@pytest.mark.parametrize("train_steps", [5, 100])
@pytest.mark.parametrize("sequence_length", [512, 8192])
@rerun_if_address_is_in_use()
def test_build_nanoset_dataloader(tp: int, dp: int, pp: int, train_steps: int, sequence_length: int):
    test_context = TestContext()

    # Create dataset files
    json_paths, mmap_dataset_paths = create_dataset_paths(tmp_dir=test_context.get_auto_remove_tmp_dir(), quantity=2)

    # Create dummy json datasets
    for idx, json_path in enumerate(json_paths):
        create_dummy_json_dataset(path_to_json=json_path, dummy_text=f"Nanoset {idx}!", n_samples=(idx + 1) * 50000)

    # Preprocess dummy json datasets
    for json_path in json_paths:
        preprocess_dummy_dataset(path_to_json=json_path)

    init_distributed(tp=tp, dp=dp, pp=pp)(_test_build_nanoset_dataloader)(
        path_to_mmap_files=mmap_dataset_paths,
        train_steps=train_steps,
        sequence_length=sequence_length,
    )


def _test_build_nanoset_dataloader(
    parallel_context: ParallelContext,
    path_to_mmap_files: str,
    train_steps: int,
    sequence_length: int,
):
    SEED = 1234
    MICRO_BATCH_SIZE = 4
    N_MICRO_BATCHES_PER_BATCH = 8
    GLOBAL_BATCH_SIZE = MICRO_BATCH_SIZE * N_MICRO_BATCHES_PER_BATCH * parallel_context.dp_pg.size()

    input_pp_rank, output_pp_rank = 0, int(parallel_context.pp_pg.size() - 1)

    # Create Nanoset configs: 1. Normal 2. Blended 3. Blended with weights
    nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0]],
        "dataset_weights": [1],
        "sequence_length": sequence_length,
        "train_split_num_samples": train_steps * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    blended_nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0], path_to_mmap_files[1]],
        "dataset_weights": None,
        "sequence_length": sequence_length,
        "train_split_num_samples": train_steps * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    blended_weighted_nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0], path_to_mmap_files[1]],
        "dataset_weights": [8, 2],
        "sequence_length": sequence_length,
        "train_split_num_samples": train_steps * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    configs = [nanoset_config, blended_nanoset_config, blended_weighted_nanoset_config]

    for config in configs:
        # Create Nanoset
        with main_rank_first(parallel_context.world_pg):
            train_dataset = Nanoset(**config)

        # Assert we have the same Nanoset in all ranks
        assert_nanoset_sync_across_all_ranks(train_dataset, parallel_context)
        # Assert Nanoset doesn't sample indexes greater than the datasets
        for idx, ds_length in enumerate(train_dataset.dataset_lengths):
            assert (
                np.max(train_dataset.dataset_sample_index, where=train_dataset.dataset_index == idx, initial=-1)
                < ds_length
            ), f"Error building Nanoset Indexes: Tryng to access sample {np.max(train_dataset.dataset_sample_index, where=train_dataset.dataset_index==idx, initial = -1)} of a {ds_length} sample dataset"

        # Create Dataloaders
        dataloader = build_nanoset_dataloader(
            train_dataset,
            sequence_length=sequence_length,
            parallel_context=parallel_context,
            input_pp_rank=input_pp_rank,
            output_pp_rank=output_pp_rank,
            micro_batch_size=MICRO_BATCH_SIZE,
            dataloader_num_workers=0,
            dataloader_drop_last=True,
        )

        # Check a batch produced by the Dataloader
        batch = next(iter(dataloader))
        assert_batch_dataloader(
            batch=batch,
            parallel_context=parallel_context,
            micro_batch_size=MICRO_BATCH_SIZE,
            sequence_length=sequence_length,
        )

    parallel_context.destroy()


@pytest.mark.parametrize(
    "tp,dp,pp",
    [
        pytest.param(*all_3d_configs)
        for gpus in range(1, min(available_gpus(), 4) + 1)
        for all_3d_configs in get_all_3d_configurations(gpus)
    ],
)
@pytest.mark.parametrize("skipped_batches", [20, 50])
@rerun_if_address_is_in_use()
def test_recover_nanoset_dataloader(tp: int, dp: int, pp: int, skipped_batches: int):
    test_context = TestContext()

    # Create dataset files
    json_paths, mmap_dataset_paths = create_dataset_paths(tmp_dir=test_context.get_auto_remove_tmp_dir(), quantity=2)

    # Create dummy json datasets
    for idx, json_path in enumerate(json_paths):
        create_dummy_json_dataset(path_to_json=json_path, dummy_text=f"Nanoset {idx}!", n_samples=(idx + 1) * 50000)

    # Preprocess dummy json datasets
    for json_path in json_paths:
        preprocess_dummy_dataset(path_to_json=json_path)

    init_distributed(tp=tp, dp=dp, pp=pp)(_test_recover_nanoset_dataloader)(
        path_to_mmap_files=mmap_dataset_paths,
        skipped_batches=skipped_batches,
    )


def _test_recover_nanoset_dataloader(
    parallel_context: ParallelContext,
    path_to_mmap_files: str,
    skipped_batches: int,
):
    SEED = 1234
    MICRO_BATCH_SIZE = 4
    N_MICRO_BATCHES_PER_BATCH = 8
    GLOBAL_BATCH_SIZE = MICRO_BATCH_SIZE * N_MICRO_BATCHES_PER_BATCH * parallel_context.dp_pg.size()
    SEQUENCE_LENGTH = 1024
    TRAIN_STEPS = 100

    input_pp_rank, output_pp_rank = 0, int(parallel_context.pp_pg.size() - 1)

    # Create Nanoset configs: 1. Normal 2. Blended 3. Blended with weights
    nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0]],
        "dataset_weights": [1],
        "sequence_length": SEQUENCE_LENGTH,
        "train_split_num_samples": TRAIN_STEPS * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    blended_nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0], path_to_mmap_files[1]],
        "dataset_weights": None,
        "sequence_length": SEQUENCE_LENGTH,
        "train_split_num_samples": TRAIN_STEPS * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    blended_weighted_nanoset_config = {
        "dataset_paths": [path_to_mmap_files[0], path_to_mmap_files[1]],
        "dataset_weights": [8, 2],
        "sequence_length": SEQUENCE_LENGTH,
        "train_split_num_samples": TRAIN_STEPS * GLOBAL_BATCH_SIZE,
        "random_seed": SEED,
    }

    configs = [nanoset_config, blended_nanoset_config, blended_weighted_nanoset_config]

    for config in configs:
        # Create Nanoset
        with main_rank_first(parallel_context.world_pg):
            train_dataset = Nanoset(**config)

        # Create initial Dataloader
        dataloader = build_nanoset_dataloader(
            train_dataset,
            sequence_length=SEQUENCE_LENGTH,
            parallel_context=parallel_context,
            input_pp_rank=input_pp_rank,
            output_pp_rank=output_pp_rank,
            micro_batch_size=MICRO_BATCH_SIZE,
            dataloader_num_workers=0,
            dataloader_drop_last=True,
        )

        # Recover from failures
        dataloader = iter(dataloader)
        for _ in range(skipped_batches + 1):  # In order to compare with the first batch of the recovered DataLoader
            batch = next(dataloader)

        # Create recover Dataloader
        recovered_dataloader = build_nanoset_dataloader(
            train_dataset,
            sequence_length=SEQUENCE_LENGTH,
            parallel_context=parallel_context,
            input_pp_rank=input_pp_rank,
            output_pp_rank=output_pp_rank,
            micro_batch_size=MICRO_BATCH_SIZE,
            dataloader_num_workers=0,
            dataloader_drop_last=True,
            # NOTE The dataloader serves batches of micro_batch_size despite of batch_accumulation_per_replica
            consumed_train_samples=skipped_batches * MICRO_BATCH_SIZE * parallel_context.dp_pg.size(),
        )

        recovered_first_batch = next(iter(recovered_dataloader))

        assert compute_batch_hash(batch) == compute_batch_hash(recovered_first_batch)

    parallel_context.destroy()
