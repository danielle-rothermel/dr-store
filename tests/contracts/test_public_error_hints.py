from __future__ import annotations

import inspect
from pathlib import Path
from typing import get_type_hints

import pytest

import dr_store
from dr_store.content_addressing import ObjectReference
from dr_store.core.reasons import (
    ContentMismatchReason,
    RegularChildFailureReason,
)
from dr_store.document_file.errors import (
    PublicationStage,
    ReadStage,
    ReplacementState,
)

PUBLIC_TYPED_ERRORS = (
    dr_store.BindingConflictError,
    dr_store.ContentHashMismatchError,
    dr_store.DocumentPublishError,
    dr_store.DocumentReadError,
    dr_store.ManifestPublishError,
    dr_store.ManifestReadError,
    dr_store.ObjectConflictError,
    dr_store.ObjectNotFoundError,
    dr_store.SchemaMismatchError,
    dr_store.SidecarVerificationError,
    dr_store.VerifiedRegularChildReadError,
)


@pytest.mark.parametrize("error_type", PUBLIC_TYPED_ERRORS)
def test_public_error_constructor_hints_resolve(
    error_type: type[BaseException],
) -> None:
    hints = get_type_hints(error_type.__init__)
    parameters = inspect.signature(error_type.__init__).parameters
    annotated_parameters = {
        name
        for name, parameter in parameters.items()
        if name != "self"
        and parameter.annotation is not inspect.Parameter.empty
    }
    assert annotated_parameters <= hints.keys()
    assert hints["return"] is type(None)

    if error_type is dr_store.DocumentPublishError:
        assert hints["path"] is Path
        assert hints["stage"] is PublicationStage
        assert hints["replacement_state"] is ReplacementState
    elif error_type is dr_store.DocumentReadError:
        assert hints["path"] is Path
        assert hints["stage"] is ReadStage
        assert hints["reason"] is RegularChildFailureReason
    elif error_type is dr_store.ManifestPublishError:
        assert hints["path"] is Path
        assert hints["stage"] is PublicationStage
        assert hints["replacement_state"] is ReplacementState
    elif error_type is dr_store.ManifestReadError:
        assert hints["path"] is Path
        assert hints["stage"] is ReadStage
        assert hints["reason"] is RegularChildFailureReason
    elif error_type is dr_store.SidecarVerificationError:
        assert hints["path"] is Path
        assert hints["reason"] is RegularChildFailureReason
    elif error_type is dr_store.VerifiedRegularChildReadError:
        assert hints["message"] is str
        assert hints["reason"] is RegularChildFailureReason
    elif error_type is dr_store.ContentHashMismatchError:
        assert hints["reason"] is ContentMismatchReason
    elif error_type is dr_store.ObjectNotFoundError:
        assert hints["reference"] is ObjectReference
    elif error_type is dr_store.BindingConflictError:
        assert hints["existing"] is ObjectReference
        assert hints["requested"] is ObjectReference
