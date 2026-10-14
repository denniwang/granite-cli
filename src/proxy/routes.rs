//! Session proxy route registration for a launch.
//!
//! One route per model the launch's enabled capabilities name, registered
//! before anything is launched at the proxy.

// Local
use crate::utils::ui::Ui;

/*-- public --*/

/// Registers one route on the session proxy per model the launch's enabled
/// capabilities name, so a launched process addressing a model by its
/// provider alias reaches the real upstream through the proxy.
///
/// The launch path calls this because it needs four things at once that no
/// single collection has: the model, the variant it was configured with, its
/// provider's real connection details, and the proxy handle. The details come
/// from the context's own sources, read through their upstream view, since a
/// provider handed out by `get` reports the proxy's own address once the
/// handle is set.
pub(crate) fn register_proxy_routes(
    ctx: &crate::AppContext,
    enabled_capabilities: &[String],
    handle: &super::ProxyHandle,
    ui: &dyn Ui,
) {
    let source = ctx.sources().models();
    for model_id in model_ids_named_by(ctx.config(), enabled_capabilities) {
        let Ok(provider) = source.upstream_for(&model_id) else {
            continue;
        };
        let Ok(model) = source.get(&model_id) else {
            continue;
        };
        let variant = crate::models::find_variant(
            model.variants(),
            source.configured_variant(&model_id).as_deref(),
        );
        let route_key = provider
            .model_alias(model_id.clone(), variant)
            .unwrap_or_else(|| model_id.clone());
        let target = super::UpstreamTarget {
            base_url: provider.base_url().to_string(),
            verify_ssl: provider.verify_ssl(),
            auth: super::UpstreamAuth::Inject(provider.api_key().cloned()),
        };
        if let Err(e) = handle.register_route(route_key, target, model_id.clone()) {
            ui.warn(&format!(
                "failed to register proxy route for model '{model_id}': {e}"
            ));
        }
    }
}

/*-- private --*/

/// The model ids the given capabilities name, read through `Validatable::refs`,
/// the one declaration of an instance's outbound names that the validator's
/// walk and the remove-time scan also read. A capability whose type is
/// unknown, or whose required dependency holds no id, names nothing here and
/// is reported by the check that runs before a launch reaches this.
fn model_ids_named_by(config: &crate::config::Config, capability_ids: &[String]) -> Vec<String> {
    use crate::config::validation::{RefKind, Validatable};

    let mut ids: Vec<String> = Vec::new();
    for capability_id in capability_ids {
        let Some(cc) = config.get_capability(capability_id) else {
            continue;
        };
        for (kind, id) in cc.refs() {
            if kind == RefKind::Model && !ids.iter().any(|seen| seen == id) {
                ids.push(id.to_string());
            }
        }
    }
    ids
}
