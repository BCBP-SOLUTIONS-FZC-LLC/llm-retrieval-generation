document.addEventListener('keydown',function(e){
  var search=document.getElementById('search');
  if(e.key==='/'&&document.activeElement!==search){e.preventDefault();search.focus();search.select();}
  if(e.key==='Escape'&&document.activeElement===search){search.blur();}
});
var _HL='.prop-name,.card-title,.card-summary,.prop-desc,.server-name,.server-host,.server-desc,.badge-version';
(function(){document.querySelectorAll(_HL).forEach(function(el){el.dataset.orig=el.textContent;});})();
function _highlight(term){
  document.querySelectorAll(_HL).forEach(function(el){
    var orig=el.dataset.orig!=null?el.dataset.orig:el.textContent;
    if(!term){el.textContent=orig;return;}
    var lower=orig.toLowerCase();
    if(lower.indexOf(term)===-1){el.textContent=orig;return;}
    var frag=document.createDocumentFragment(),rem=orig,lrem=lower;
    while(lrem.indexOf(term)!==-1){
      var i=lrem.indexOf(term);
      if(i>0)frag.appendChild(document.createTextNode(rem.slice(0,i)));
      var m=document.createElement('mark');m.className='search-mark';
      m.textContent=rem.slice(i,i+term.length);
      frag.appendChild(m);
      rem=rem.slice(i+term.length);lrem=rem.toLowerCase();
    }
    if(rem)frag.appendChild(document.createTextNode(rem));
    el.textContent='';el.appendChild(frag);
  });
}
var _searchT;
document.getElementById('search').addEventListener('input',function(e){
  clearTimeout(_searchT);
  var val=e.target.value.toLowerCase();
  _searchT=setTimeout(function(){
    document.querySelectorAll('.card').forEach(function(card){
      card.style.display=card.innerText.toLowerCase().includes(val)||!val?'':'none';
    });
    _highlight(val);
  },120);
});
function copyText(text){
  navigator.clipboard.writeText(text).then(function(){
    var t=document.createElement('div');
    t.textContent='Copied!';
    t.style.cssText='position:fixed;bottom:72px;right:24px;background:linear-gradient(to right,#8426b0,#bd0283);color:#fff;padding:6px 14px;border-radius:6px;font-size:.78rem;font-weight:600;font-family:"Poppins",sans-serif;box-shadow:0 4px 12px rgba(132,38,176,.4);pointer-events:none;z-index:999;opacity:1;transition:opacity .3s';
    document.body.appendChild(t);
    setTimeout(function(){t.style.opacity='0';},700);
    setTimeout(function(){t.remove();},1000);
  });
}
function setCardExpanded(card,expanded){
  var header=card.querySelector('.card-header');
  var body=card.querySelector('.card-body');
  var btn=card.querySelector('.toggle-btn');
  if(!body)return;
  body.style.display=expanded?'':'none';
  if(btn)btn.textContent=expanded?'▾':'▸';
  if(header)header.setAttribute('aria-expanded',expanded?'true':'false');
}
function toggleCard(header){
  var body=header.closest('.card').querySelector('.card-body');
  setCardExpanded(header.closest('.card'),body.style.display==='none');
}
function expandSchemaCard(schemaId){
  var card=document.getElementById(schemaId);
  if(card)setCardExpanded(card,true);
}
document.querySelectorAll('.card-header').forEach(function(header){
  header.addEventListener('click',function(){toggleCard(header);});
  header.addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key===' '){e.preventDefault();toggleCard(header);}
  });
  header.setAttribute('role','button');
  header.setAttribute('tabindex','0');
  header.setAttribute('aria-expanded','false');
});
document.querySelectorAll('a[href^="#schema-"]').forEach(function(a){
  a.addEventListener('click',function(){
    var id=a.getAttribute('href').slice(1);
    setTimeout(function(){expandSchemaCard(id);},0);
  });
});
function deepLinkField(hash){
  var m=hash.match(/^#schema-([^.]+)\.(.+)$/);
  if(!m)return;
  var rowId='field--'+m[1]+'--'+m[2];
  var row=document.getElementById(rowId);
  if(!row)return;
  var card=row.closest('.card');
  if(card)setCardExpanded(card,true);
  setTimeout(function(){
    row.scrollIntoView({behavior:'smooth',block:'center'});
    row.classList.remove('field-highlight');
    void row.offsetWidth;
    row.classList.add('field-highlight');
  },120);
}
window.addEventListener('hashchange',function(){
  var h=window.location.hash;
  if(h.indexOf('#schema-')===0){
    if(h.indexOf('.')!==-1){deepLinkField(h);}
    else{expandSchemaCard(h.slice(1));}
  }
});
if(window.location.hash){deepLinkField(window.location.hash);}
function toggleTheme(){
  var html=document.documentElement;
  var next=html.getAttribute('data-theme')==='light'?'dark':'light';
  html.setAttribute('data-theme',next);
  var btn=document.getElementById('theme-btn');
  if(btn)btn.textContent=next==='light'?'☀️':'🌙';
  try{localStorage.setItem('asyncapi-theme',next);}catch(e){}
}
(function(){
  var saved;try{saved=localStorage.getItem('asyncapi-theme');}catch(e){}
  if(saved==='light'){
    document.documentElement.setAttribute('data-theme','light');
    var btn=document.getElementById('theme-btn');
    if(btn)btn.textContent='☀️';
  }
})();
(function(){
  var links=document.querySelectorAll('.sidebar a[href^="#"]');
  function activate(){
    var fromTop=window.scrollY+80;
    var active=null;
    links.forEach(function(link){
      var section=document.getElementById(link.getAttribute('href').slice(1));
      if(section&&section.offsetTop<=fromTop&&section.offsetTop+section.offsetHeight>fromTop){
        active=link;
      }
      link.style.cssText='';
    });
    if(active){
      active.style.background='linear-gradient(to right,#8426b0 3%,#bd0283 47%,#ec4b3c 98%)';
      active.style.webkitBackgroundClip='text';
      active.style.webkitTextFillColor='transparent';
      active.style.backgroundClip='text';
      active.style.borderLeftColor='#8426b0';
    }
  }
  window.addEventListener('scroll',activate,{passive:true});
  activate();
})();
window.addEventListener('beforeunload',function(){
  try{localStorage.setItem('asyncapi-scrollY',window.scrollY);}catch(e){}
});
(function(){
  var y;try{y=localStorage.getItem('asyncapi-scrollY');}catch(e){}
  if(y&&!window.location.hash){window.scrollTo(0,parseInt(y,10));}
})();
