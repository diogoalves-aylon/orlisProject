from django.urls import path
from .views import FrontPagesView

urlpatterns = [
    path("", FrontPagesView.as_view(template_name="landing_page.html"), name="landing-page"),
    path("pricing/", FrontPagesView.as_view(template_name="pricing_page.html"), name="pricing-page"),
    path("payment/", FrontPagesView.as_view(template_name="payment_page.html"), name="payment-page"),
    path("checkout/", FrontPagesView.as_view(template_name="checkout_page.html"), name="checkout-page"),
    path("help_center/", FrontPagesView.as_view(template_name="help_center_landing.html"), name="help-center-landing"),
    path("help_center/article/", FrontPagesView.as_view(template_name="help_center_article.html"), name="help-center-article"),
]
