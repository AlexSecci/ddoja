document.getElementById('practice-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const name=document.getElementById('practice-name').value;
  const email=document.getElementById('practice-email').value;
  const sponsorship=document.getElementById('practice-sponsorship').value;
  const file=document.getElementById('practice-resume').files[0];
  try {
    if(!file) throw new Error('Select a fictional test file.');
    if(file.size>10000) throw new Error('Practice files must be below 10 KB.');
    const session=await fetch('/api/session').then(r=>r.json());
    const response=await fetch('/api/practice/receipt',{method:'POST',headers:{'Content-Type':'application/json','X-Pipeline-Token':session.token},body:JSON.stringify({name,email,sponsorship,filename:file.name,file_content:await file.text()})});
    const receipt=await response.json();
    if(!response.ok) throw new Error(receipt.detail||'Local test failed.');
    document.getElementById('practice-form').classList.add('hidden');
    document.getElementById('practice-confirmation').classList.remove('hidden');
    document.getElementById('receipt-id').textContent='Receipt: '+receipt.receipt_id;
    document.getElementById('receipt-detail').textContent=JSON.stringify(receipt,null,2);
  } catch(error) {document.getElementById('status').textContent=error.message;}
});
