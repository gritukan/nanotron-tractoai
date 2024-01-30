import pytest
import torch
import torch.distributed as dist
import torch.nn.functional as F
from helpers.utils import (
    init_distributed,
)
from nanotron.doremi.doremi_context import DoReMiContext
from nanotron.doremi.loss import CrossEntropyWithPerDomainLoss, DoReMiLossForProxyTraining, compute_per_domain_loss
from nanotron.parallel import ParallelContext
from nanotron.parallel.tensor_parallel.functional import sharded_cross_entropy
from nanotron.sanity_checks import assert_tensor_synced_across_pg


@pytest.fixture
def doremi_context():
    N_DOMAINS = 5
    domain_keys = [f"domain {i}" for i in range(N_DOMAINS)]
    domain_weights = F.softmax(torch.ones(N_DOMAINS, requires_grad=False), dim=-1)
    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)
    return doremi_context


def get_partition_logit(logits, parallel_context):
    tp_size = dist.get_world_size(parallel_context.tp_pg)
    tp_rank = dist.get_rank(parallel_context.tp_pg)
    VOCAB_SIZE = logits.shape[-1]
    per_partition = VOCAB_SIZE // tp_size
    chunks = torch.split(logits, per_partition, dim=-1)
    return chunks[tp_rank]


@pytest.mark.parametrize("tp", [1, 2])
def test_computing_per_token_loss(tp: int):
    BATCH_SIZE = 512
    SEQ_LEN = 128
    VOCAB_SIZE = 4

    torch.manual_seed(69)

    logits = torch.randn(BATCH_SIZE, SEQ_LEN, VOCAB_SIZE)
    targets = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, SEQ_LEN))

    ref_losses = F.cross_entropy(logits.view(-1, logits.size(2)), targets.view(-1), reduction="none")

    init_distributed(tp=tp, dp=1, pp=1)(_test_computing_per_token_loss)(
        logits=logits, targets=targets, ref_losses=ref_losses
    )


def _test_computing_per_token_loss(parallel_context: ParallelContext, logits, targets, ref_losses):
    logits = logits.to("cuda")
    targets = targets.to("cuda")
    parallel_logits = get_partition_logit(logits, parallel_context)

    loss = sharded_cross_entropy(parallel_logits, targets, parallel_context.tp_pg)

    assert torch.allclose(loss.cpu().view(-1), ref_losses)


@pytest.mark.parametrize("dp", [1, 2])
def test_doremi_loss(dp: int):
    GLOBAL_BATCH_SIZE = 512
    BATCH_SIZE = GLOBAL_BATCH_SIZE // dp
    SEQ_LEN = 128
    N_DOMAINS = 5
    domain_keys = [f"domain {i}" for i in range(N_DOMAINS)]
    DOMAIN_WEIGHTS = F.softmax(torch.ones(N_DOMAINS, requires_grad=False), dim=-1)

    init_distributed(tp=1, dp=dp, pp=1)(_test_doremi_loss)(
        global_batch_size=GLOBAL_BATCH_SIZE,
        batch_size=BATCH_SIZE,
        seq_len=SEQ_LEN,
        domain_keys=domain_keys,
        domain_weights=DOMAIN_WEIGHTS,
    )


def _test_doremi_loss(
    parallel_context: ParallelContext, global_batch_size, batch_size, seq_len, domain_keys, domain_weights
):
    N_DOMAINS = domain_weights.shape[0]
    domain_weights = domain_weights.to("cuda")
    initial_domain_weights = domain_weights.clone()
    losses = torch.randn(batch_size, seq_len, device="cuda")
    ref_losses = torch.randn(batch_size, seq_len, device="cuda")
    domain_idxs = torch.randint(0, N_DOMAINS, (batch_size,), device="cuda")

    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)
    loss_func = DoReMiLossForProxyTraining(doremi_context, parallel_context)

    excess_loss, domain_losses, domain_weights = loss_func(losses, ref_losses, domain_idxs)

    # NOTE: no values in excess_loss should be negative
    assert (excess_loss >= 0.0).all()
    assert excess_loss.shape == (global_batch_size, seq_len)
    assert_tensor_synced_across_pg(
        excess_loss, parallel_context.dp_pg, msg=lambda err: f"Excess losses are not synced across ranks {err}"
    )

    assert (domain_losses > 0.0).all()
    assert domain_losses.shape == (N_DOMAINS,)
    assert_tensor_synced_across_pg(
        domain_losses, parallel_context.dp_pg, msg=lambda err: f"Domain losses are not synced across ranks {err}"
    )

    assert (domain_weights > 0.0).all()
    assert domain_weights.shape == (N_DOMAINS,)
    assert not torch.allclose(initial_domain_weights, domain_weights)
    assert torch.allclose(domain_weights.sum(dim=-1), torch.tensor(1.0))
    # NOTE: check if the loss function updates the domain weights in the doremi context
    assert torch.allclose(doremi_context.domain_weights, domain_weights)
    assert_tensor_synced_across_pg(
        domain_weights, parallel_context.dp_pg, msg=lambda err: f"Domain weights are not synced across ranks {err}"
    )


@pytest.mark.parametrize("dp", [1, 2])
def test_computing_per_domain_loss(dp: int):
    GLOBAL_BATCH_SIZE = 512
    BATCH_SIZE = GLOBAL_BATCH_SIZE // dp
    SEQ_LEN = 128
    N_DOMAINS = 5

    domain_keys = [f"domain {i}" for i in range(N_DOMAINS)]
    domain_weights = F.softmax(torch.ones(N_DOMAINS, requires_grad=False), dim=-1)

    init_distributed(tp=1, dp=dp, pp=1)(_test_computing_per_domain_loss)(
        batch_size=BATCH_SIZE,
        seq_len=SEQ_LEN,
        domain_keys=domain_keys,
        domain_weights=domain_weights,
    )


def _test_computing_per_domain_loss(
    parallel_context: ParallelContext, batch_size, seq_len, domain_keys, domain_weights
):
    N_DOMAINS = domain_weights.shape[0]
    domain_weights = domain_weights.to("cuda")
    losses = torch.randn(batch_size, seq_len, device="cuda")
    domain_idxs = torch.randint(0, N_DOMAINS, (batch_size,), device="cuda")

    doremi_context = DoReMiContext(domain_weights, domain_keys, is_proxy=False)

    per_domain_loss = compute_per_domain_loss(losses, domain_idxs, doremi_context, parallel_context)

    assert per_domain_loss.shape == (N_DOMAINS,)
    assert_tensor_synced_across_pg(
        per_domain_loss, parallel_context.dp_pg, msg=lambda err: f"Per domain loss are not synced across ranks {err}"
    )


# @pytest.mark.parametrize("tp", [1, 2])
# def test_cross_entropy_with_per_domain_loss(tp: int, doremi_context):
#     BATCH_SIZE = 512
#     SEQ_LEN = 128
#     VOCAB_SIZE = 4
#     torch.manual_seed(69)

#     logits = torch.randn(BATCH_SIZE, SEQ_LEN, VOCAB_SIZE)
#     label_ids = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, SEQ_LEN))
#     label_mask = torch.ones((BATCH_SIZE, SEQ_LEN), dtype=torch.bool)

#     ref_losses = F.cross_entropy(logits.view(-1, logits.size(2)), label_ids.view(-1), reduction="none")

#     init_distributed(tp=tp, dp=1, pp=1)(_test_cross_entropy_with_per_domain_loss)(
#         logits=logits, label_ids=label_ids, label_mask=label_mask, ref_losses=ref_losses, doremi_context=doremi_context, batch_size=BATCH_SIZE
#     )


# def _test_cross_entropy_with_per_domain_loss(parallel_context: ParallelContext, logits, label_ids, label_mask, ref_losses, batch_size, doremi_context):
#     N_DOMAINS = doremi_context.num_domains

#     logits = logits.to("cuda")
#     label_ids = label_ids.to("cuda")
#     label_mask = label_mask.to("cuda")
#     parallel_logits = get_partition_logit(logits, parallel_context)
#     domain_idxs = torch.randint(0, N_DOMAINS, (batch_size,), device="cuda")

#     loss_func = CrossEntropyWithPerDomainLoss(doremi_context, parallel_context)
#     outputs = loss_func(parallel_logits, label_ids, label_mask, domain_idxs)

#     assert torch.allclose(outputs["loss"].cpu().view(-1), ref_losses)
#     assert 1 == 1


@pytest.mark.parametrize("tp", [1, 2])
def test_cross_entropy_with_per_domain_loss(tp: int, doremi_context):
    BATCH_SIZE = 512
    SEQ_LEN = 128
    VOCAB_SIZE = 4
    N_DOMAINS = doremi_context.num_domains

    torch.manual_seed(69)

    logits = torch.randn(BATCH_SIZE, SEQ_LEN, VOCAB_SIZE)
    label_ids = torch.randint(0, VOCAB_SIZE, (BATCH_SIZE, SEQ_LEN))
    label_mask = torch.ones((BATCH_SIZE, SEQ_LEN), dtype=torch.bool)
    domain_idxs = torch.randint(0, N_DOMAINS, (BATCH_SIZE,))

    ref_losses = F.cross_entropy(logits.view(-1, logits.size(2)), label_ids.view(-1))

    init_distributed(tp=tp, dp=1, pp=1)(_test_cross_entropy_with_per_domain_loss)(
        logits=logits,
        label_ids=label_ids,
        label_mask=label_mask,
        domain_idxs=domain_idxs,
        ref_losses=ref_losses,
        doremi_context=doremi_context,
    )


def _test_cross_entropy_with_per_domain_loss(
    parallel_context: ParallelContext, logits, label_ids, label_mask, domain_idxs, ref_losses, doremi_context
):
    logits = logits.to("cuda")
    label_ids = label_ids.to("cuda")
    label_mask = label_mask.to("cuda")
    domain_idxs = domain_idxs.to("cuda")

    parallel_logits = get_partition_logit(logits, parallel_context)

    # loss = sharded_cross_entropy(parallel_logits, label_ids, parallel_context.tp_pg)
    loss_func = CrossEntropyWithPerDomainLoss(doremi_context, parallel_context)
    outputs = loss_func(parallel_logits, label_ids, label_mask, domain_idxs)

    assert torch.allclose(outputs["loss"].cpu().view(-1), ref_losses)
    assert outputs["domain_losses"].shape == (doremi_context.num_domains,)
