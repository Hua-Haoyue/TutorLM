import pytest

from backend.application.services.learning_obstacles import (
    apply_domain_policy, classify_root_domain, explanation_policy, validate_domain_answer,
)
from backend.domain.errors import ProviderError


@pytest.mark.parametrize('title,selection,expected', [
    ('做菜步骤', '锅里加热糖', 'practical'),
    ('数学证明', '矩阵定理', 'academic'),
    ('未知主题', '', 'practical'),
])
def test_root_scene(title, selection, expected):
    assert classify_root_domain(title=title, selection=selection)['domain'] == expected


def test_explicit_rigor_overrides_practical_scene():
    result = classify_root_domain(title='做菜步骤', question='请给出严谨科研解释')
    assert result['domain'] == 'academic'
    assert result['override'] == 'rigorous_request'


def test_low_confidence_and_output_check():
    context = {'explanation_policy': explanation_policy(1)}
    apply_domain_policy(context)
    assert context['domain_context']['confidence'] == 'low'
    validate_domain_answer('加热后表面变成金黄色，香味更明显。', context)
    for answer in ('含氮化合物参与反应。', '这是一种复杂反应。'):
        with pytest.raises(ProviderError):
            validate_domain_answer(answer, context)
    context['domain_context']['domain'] = 'academic'
    validate_domain_answer('含氮化合物参与反应。', context)
