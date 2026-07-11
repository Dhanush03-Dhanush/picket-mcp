import pytest
from pydantic import ValidationError

from picket.core.errors import ErrorCode, failure
from picket.core.models import CadenceSpec, EndpointSpec, PredicateSpec


def test_good_specs_validate():
    EndpointSpec(url="https://example.com/spx", auth_ref="SPX_TOKEN")
    PredicateSpec(path="$.last", op="lt", value=4800)
    CadenceSpec(interval_seconds=30)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EndpointSpec(url="ftp://nope"),  # bad scheme
        lambda: PredicateSpec(path="$.last", op="between", value=1),  # unknown op
        lambda: PredicateSpec(path="$.last", op="lt"),  # threshold op needs a value
        lambda: CadenceSpec(interval_seconds=0),  # must be > 0
    ],
)
def test_malformed_specs_rejected(factory):
    with pytest.raises(ValidationError):
        factory()


def test_validation_error_maps_to_invalid_spec_envelope():
    try:
        CadenceSpec(interval_seconds=-1)
    except ValidationError as err:
        env = failure(ErrorCode.INVALID_SPEC, str(err))
    assert env == {"ok": False, "error_code": "INVALID_SPEC", "message": env["message"]}
    assert env["message"]
