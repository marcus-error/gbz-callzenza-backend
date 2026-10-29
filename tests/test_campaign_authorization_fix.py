import pytest
from fastapi import HTTPException, status
from app.db.models import Campaign, User
from app.api.routes.campaigns import check_campaign_access, DEFAULT_BUSINESS_ID

def test_authorization_test_a_same_org_user_allowed():
    """Test A: User in Org A accessing Campaign in Org A is ALLOWED."""
    user = User(id="user_a", role="ADMIN", business_id="org_a")
    campaign = Campaign(id="camp_a", business_id="org_a", user_id="user_a")
    # Should not raise any exception
    check_campaign_access(campaign, user)

def test_authorization_test_b_cross_org_user_forbidden():
    """Test B: User in Org A accessing Campaign in Org B is FORBIDDEN (403)."""
    user = User(id="user_a", role="ADMIN", business_id="org_a")
    campaign = Campaign(id="camp_b", business_id="org_b", user_id="user_b")
    with pytest.raises(HTTPException) as exc_info:
        check_campaign_access(campaign, user)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN
    assert "Forbidden: You do not have access to this campaign" in str(exc_info.value.detail)

def test_authorization_test_c_default_tenant_admin_allowed():
    """Test C: Admin user with empty/null business_id accessing default tenant campaign is ALLOWED."""
    user = User(id="admin_1", role="ADMIN", business_id=None)
    campaign = Campaign(id="camp_default", business_id=DEFAULT_BUSINESS_ID, user_id="admin_1")
    # Should not raise any exception
    check_campaign_access(campaign, user)

def test_authorization_test_d_restricted_user_cross_org_forbidden():
    """Test D: User without campaign permission accessing restricted cross-org campaign is FORBIDDEN (403)."""
    user = User(id="user_x", role="USER", business_id="org_a")
    campaign = Campaign(id="camp_y", business_id="org_b", user_id="user_other")
    with pytest.raises(HTTPException) as exc_info:
        check_campaign_access(campaign, user)
    assert exc_info.value.status_code == status.HTTP_403_FORBIDDEN

def test_authorization_super_admin_allowed_everywhere():
    """Super admin can access campaigns in any organization."""
    user = User(id="sa_1", role="SUPER_ADMIN", business_id="org_a")
    campaign = Campaign(id="camp_z", business_id="org_b", user_id="user_z")
    # Should not raise any exception
    check_campaign_access(campaign, user)
