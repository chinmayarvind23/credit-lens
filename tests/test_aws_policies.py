"""Policy rendering keeps routine deployment separate from trusted account bootstrap."""

import json
import runpy
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/render_aws_policies.py"


def test_unbootstrapped_render_grants_no_cloudfront_ownership() -> None:
    """An unknown distribution cannot become an account-wide mutation or tagging permission."""
    render = runpy.run_path(str(SCRIPT))["render_policies"]
    with tempfile.TemporaryDirectory() as temporary:
        paths = render("111122223333", Path(temporary) / "policies")
        assert len(paths) == 5
        for path in paths:
            policy = json.loads(path.read_text())
            assert "ACCOUNT_ID" not in path.read_text()
            assert "DISTRIBUTION_ID" not in path.read_text()
            if path.name.startswith("deployer-"):
                assert len(json.dumps(policy, separators=(",", ":"))) <= 6144
                for statement in policy["Statement"]:
                    actions = statement["Action"]
                    actions = [actions] if isinstance(actions, str) else actions
                    assert not any(action.startswith("cloudfront:") for action in actions)
                    assert not any(action in ("*", "iam:*") for action in actions)


def test_routine_deployer_cannot_escalate_roles_or_claim_another_distribution() -> None:
    """Pin the distribution ARN and limit IAM writes to one scoped PassRole."""
    render = runpy.run_path(str(SCRIPT))["render_policies"]
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "policies"
        render("111122223333", output, "E1234567890ABC")
        service = json.loads((output / "deployer-service.json").read_text())
        edge = json.loads((output / "deployer-edge.json").read_text())
        iam = [s for s in service["Statement"] if str(s["Action"]).startswith("iam:")]
        assert {s["Action"] for s in iam} == {"iam:GetRole", "iam:PassRole"}
        assert all(
            s["Resource"] == "arn:aws:iam::111122223333:role/creditlens-demo-execution" for s in iam
        )
        passing = next(s for s in iam if s["Action"] == "iam:PassRole")
        assert (
            passing["Condition"]["StringEquals"]["iam:PassedToService"] == "ecs-tasks.amazonaws.com"
        )
        distribution = next(
            s for s in edge["Statement"] if s["Sid"] == "ManageExistingDemoDistribution"
        )
        assert (
            distribution["Resource"]
            == "arn:aws:cloudfront::111122223333:distribution/E1234567890ABC"
        )
        assert "cloudfront:TagResource" not in distribution["Action"]
        assert "cloudfront:CreateDistribution" not in distribution["Action"]


def test_new_tagged_rules_require_owned_parent_security_group() -> None:
    """Permit requested rule tags while requiring existing ownership on the parent group."""
    render = runpy.run_path(str(SCRIPT))["render_policies"]
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / "policies"
        render("111122223333", output)
        policy = json.loads((output / "deployer-network.json").read_text())
        rules = next(
            s for s in policy["Statement"] if s["Sid"] == "CreateSecurityGroupRulesWithDemoTags"
        )
        assert rules["Resource"] == "arn:aws:ec2:us-east-1:111122223333:security-group-rule/*"
        assert set(rules["Action"]) == {
            "ec2:AuthorizeSecurityGroupIngress",
            "ec2:AuthorizeSecurityGroupEgress",
        }
        assert rules["Condition"]["StringEquals"] == {
            "aws:RequestTag/Project": "CreditLens",
            "aws:RequestTag/Environment": "synthetic-demo",
        }
        parent = next(s for s in policy["Statement"] if s["Sid"] == "ManageOwnedNetworkResources")
        assert "arn:aws:ec2:us-east-1:111122223333:security-group/*" in parent["Resource"]
        assert set(rules["Action"]).issubset(parent["Action"])
        assert parent["Condition"]["StringEquals"] == {
            "ec2:ResourceTag/Project": "CreditLens",
            "ec2:ResourceTag/Environment": "synthetic-demo",
        }


@pytest.mark.parametrize("account,distribution", [("*", None), ("111122223333", "*")])
def test_policy_identity_rejects_wildcards(account: str, distribution: str | None) -> None:
    """Identity parameters cannot expand an ARN boundary into a broader policy."""
    render = runpy.run_path(str(SCRIPT))["render_policies"]
    with tempfile.TemporaryDirectory() as temporary:
        with pytest.raises(ValueError):
            render(account, Path(temporary) / "policies", distribution)
