from django import forms
from .models import Cliente

class ClienteAdminForm(forms.ModelForm):
    logo_file = forms.ImageField(required=False, label="Logo (upload)")

    class Meta:
        model = Cliente
        fields = ['idCliente', 'nome', 'email']

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.cleaned_data.get('logo_file'):
            instance.logo = self.cleaned_data['logo_file'].read()
        if commit:
            instance.save()
        return instance
